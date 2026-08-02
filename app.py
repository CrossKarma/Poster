# Poster - public web version.
# Users sign in with YouTube (Google OAuth), Poster generates titles,
# descriptions, hashtags and captions with Claude on the backend, and the
# browser uploads the video straight to YouTube with the user's own token.
#
# Environment variables it needs (set these in Render):
#   SECRET_KEY            random string for sessions
#   GOOGLE_CLIENT_ID      from Google Cloud OAuth client
#   GOOGLE_CLIENT_SECRET  from Google Cloud OAuth client
#   ANTHROPIC_API_KEY     your Claude API key (users never see it)
#   APP_URL               e.g. https://posterai.studio  (no trailing slash)
#   DATABASE_URL          optional, Render Postgres; falls back to SQLite
#   TRIAL_DAYS            optional, default 7
#   STRIPE_SECRET_KEY     optional until payments go live
#   STRIPE_PRICE_ID       optional, the $10/month price id
#   STRIPE_WEBHOOK_SECRET optional, from the Stripe webhook endpoint

import os
import json
import time
import secrets
import datetime
import urllib.parse
import urllib.request

from flask import Flask, request, session, redirect, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy

APP_URL = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if APP_URL.startswith("https://"):
    app.config["SESSION_COOKIE_SECURE"] = True

db_url = os.environ.get("DATABASE_URL", "sqlite:///poster.db")
if db_url.startswith("postgres://"):  # Render's URL format vs SQLAlchemy's
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(255))
    channel_title = db.Column(db.String(255))
    refresh_token = db.Column(db.Text)
    niche = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    trial_ends = db.Column(db.DateTime)
    subscribed = db.Column(db.Boolean, default=False)
    stripe_customer_id = db.Column(db.String(64))
    generations = db.Column(db.Integer, default=0)


with app.app_context():
    db.create_all()


# ---------- small helpers ----------

def http_json(url, data=None, headers=None, method=None):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None


def access_status(u):
    if u.subscribed:
        return "subscribed"
    if u.trial_ends and datetime.datetime.utcnow() < u.trial_ends:
        return "trial"
    return "expired"


def require_access():
    u = current_user()
    if not u:
        return None, (jsonify({"ok": False, "error": "not signed in"}), 401)
    if access_status(u) == "expired":
        return None, (jsonify({"ok": False, "error": "trial expired"}), 402)
    return u, None


# ---------- pages ----------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


# ---------- Google / YouTube OAuth ----------

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly openid email"


@app.get("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    q = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": APP_URL + "/oauth/callback",
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + q)


@app.get("/oauth/callback")
def oauth_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        return "State mismatch, try signing in again.", 400
    code = request.args.get("code")
    if not code:
        return redirect("/")
    tok = http_json("https://oauth2.googleapis.com/token",
                    data=urllib.parse.urlencode({
                        "code": code,
                        "client_id": os.environ["GOOGLE_CLIENT_ID"],
                        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                        "redirect_uri": APP_URL + "/oauth/callback",
                        "grant_type": "authorization_code",
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    info = http_json("https://openidconnect.googleapis.com/v1/userinfo",
                     headers={"Authorization": "Bearer " + access})
    gid = info.get("sub")
    if not gid:
        return "Could not read your Google account.", 400

    channel_title = ""
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                       headers={"Authorization": "Bearer " + access})
        items = ch.get("items") or []
        if items:
            channel_title = items[0]["snippet"]["title"]
    except Exception:
        pass

    u = User.query.filter_by(google_id=gid).first()
    if not u:
        u = User(google_id=gid,
                 trial_ends=datetime.datetime.utcnow() + datetime.timedelta(days=TRIAL_DAYS))
        db.session.add(u)
    u.email = info.get("email") or u.email
    u.channel_title = channel_title or u.channel_title
    if refresh:
        u.refresh_token = refresh
    db.session.commit()
    session["uid"] = u.id
    session.permanent = True
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"signedIn": False})
    status = access_status(u)
    days_left = 0
    if u.trial_ends:
        days_left = max(0, (u.trial_ends - datetime.datetime.utcnow()).days)
    return jsonify({
        "signedIn": True,
        "email": u.email,
        "channel": u.channel_title,
        "niche": u.niche or "",
        "status": status,
        "trialDaysLeft": days_left,
        "billingEnabled": bool(os.environ.get("STRIPE_SECRET_KEY")),
    })


@app.post("/api/niche")
def save_niche():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    u.niche = (request.get_json(silent=True) or {}).get("niche", "")[:2000]
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/youtube-token")
def youtube_token():
    # Turns the stored refresh token into a fresh access token so the
    # browser can upload the video straight to YouTube. The video file
    # never touches this server.
    u, err = require_access()
    if err:
        return err
    if not u.refresh_token:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    try:
        tok = http_json("https://oauth2.googleapis.com/token",
                        data=urllib.parse.urlencode({
                            "client_id": os.environ["GOOGLE_CLIENT_ID"],
                            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                            "refresh_token": u.refresh_token,
                            "grant_type": "refresh_token",
                        }).encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    except Exception:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    if not tok.get("access_token"):
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    return jsonify({"ok": True, "accessToken": tok["access_token"],
                    "expiresIn": tok.get("expires_in", 3600)})


# ---------- Claude metadata generation ----------

CATEGORIES = ["horror", "story", "funny", "gaming", "satisfying", "educational", "other"]

GEN_PROMPT = """You are writing the listing copy for one short-form video.
The still frames above were sampled from the video in order. Describe only what
is visibly in the frames; if they are dark or ambiguous, stay safe rather than
inventing a story beat you cannot see.

Creator's niche: {niche}
Original file name: {filename}

Respond with ONLY a JSON object, no preamble, no markdown fences, shaped exactly:
{{
 "category": one of {cats},
 "title": "under 90 characters, no promise the video does not deliver",
 "description": "2 to 4 sentences, first line works alone as a hook",
 "hashtags": ["8 to 12 tags, no # symbol, lowercase, specific before generic"],
 "tiktok": "one TikTok caption, hook first, 3 to 5 #hashtags at the end, under 150 characters",
 "instagram": "one Instagram Reels caption, a touch more descriptive, 5 to 8 #hashtags at the end",
 "tweet": "one post for X, under 260 characters including 1 to 3 #hashtags"
}}"""


@app.post("/api/generate")
def generate():
    u, err = require_access()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    frames = data.get("frames") or []
    filename = str(data.get("filename") or "")[:200]
    if not frames:
        return jsonify({"ok": False, "error": "no frames"}), 400

    content = []
    for f in frames[:6]:
        b64 = str(f).split(",", 1)[-1]
        if len(b64) < 100:
            continue
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    if not content:
        return jsonify({"ok": False, "error": "frames unreadable"}), 400
    content.append({"type": "text",
                    "text": GEN_PROMPT.format(niche=(u.niche or "general short-form content"),
                                              filename=filename,
                                              cats=json.dumps(CATEGORIES))})
    try:
        resp = http_json("https://api.anthropic.com/v1/messages",
                         data={"model": "claude-sonnet-4-6",
                               "max_tokens": 1000,
                               "messages": [{"role": "user", "content": content}]},
                         headers={"Content-Type": "application/json",
                                  "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        out = json.loads(text)
    except Exception:
        return jsonify({"ok": False, "error": "generation failed, try again"}), 502

    u.generations = (u.generations or 0) + 1
    db.session.commit()
    out["ok"] = True
    if out.get("category") not in CATEGORIES:
        out["category"] = "other"
    return jsonify(out)


# ---------- Stripe (activates once the keys are set) ----------

@app.post("/api/checkout")
def checkout():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    key = os.environ.get("STRIPE_SECRET_KEY")
    price = os.environ.get("STRIPE_PRICE_ID")
    if not key or not price:
        return jsonify({"ok": False, "error": "billing not set up yet"}), 501
    form = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": APP_URL + "/?paid=1",
        "cancel_url": APP_URL + "/",
        "client_reference_id": str(u.id),
    }
    if u.email:
        form["customer_email"] = u.email
    sess = http_json("https://api.stripe.com/v1/checkout/sessions",
                     data=urllib.parse.urlencode(form).encode(),
                     headers={"Authorization": "Bearer " + key,
                              "Content-Type": "application/x-www-form-urlencoded"})
    return jsonify({"ok": True, "url": sess.get("url")})


@app.post("/api/stripe-webhook")
def stripe_webhook():
    # Minimal webhook: marks users subscribed on checkout completion and
    # unsubscribed when the subscription is cancelled. Signature checking
    # uses the raw payload with Stripe's scheme.
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    payload = request.get_data()
    if secret:
        import hmac, hashlib
        sig = request.headers.get("Stripe-Signature", "")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        signed = (parts.get("t", "") + "." ).encode() + payload
        expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, parts.get("v1", "")):
            return "bad signature", 400
    event = json.loads(payload.decode() or "{}")
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        uid = obj.get("client_reference_id")
        u = db.session.get(User, int(uid)) if uid else None
        if u:
            u.subscribed = True
            u.stripe_customer_id = obj.get("customer") or u.stripe_customer_id
            db.session.commit()
    elif kind in ("customer.subscription.deleted", "invoice.payment_failed"):
        cust = obj.get("customer")
        if cust:
            u = User.query.filter_by(stripe_customer_id=cust).first()
            if u:
                u.subscribed = False
                db.session.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
