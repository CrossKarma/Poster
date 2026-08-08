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
import re
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
# All analysis reasons about times in the creator's timezone. Eastern for now.
from zoneinfo import ZoneInfo
APP_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))

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
    default_privacy = db.Column(db.String(16), default="public")
    model_pref = db.Column(db.String(16), default="quality")
    retention_days = db.Column(db.Integer, default=3)   # 0 means forever
    post_time = db.Column(db.String(8), default="18:00")
    posts_per_day = db.Column(db.Integer, default=1)
    analysis = db.Column(db.Text)        # last channel analysis JSON; fed back into generation
    schedule_json = db.Column(db.Text)   # weekly posting schedule {"mon":["18:00"],...}
    active_channel_id = db.Column(db.Integer)  # which Channel row is selected


class Channel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    google_id = db.Column(db.String(64))       # the Google identity that granted access
    channel_id = db.Column(db.String(64))      # the UC... YouTube channel id
    title = db.Column(db.String(255), default="")
    refresh_token = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    # Everything below is strictly this channel's own data. Channels on the
    # same account never see each other's analysis, niche, or schedule.
    niche = db.Column(db.Text, default="")
    analysis = db.Column(db.Text)
    schedule_json = db.Column(db.Text)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    channel_pk = db.Column(db.Integer, index=True)   # Channel row this was posted to
    video_id = db.Column(db.String(32))
    title = db.Column(db.String(255), default="")
    format = db.Column(db.String(16), default="video")
    publish_at = db.Column(db.String(40))   # ISO string if scheduled, else empty
    crossed = db.Column(db.String(64), default="")  # comma list: tiktok,instagram,x
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Meta(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255), default="")


class Idea(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    channel_pk = db.Column(db.Integer, index=True)   # Channel row this idea belongs to
    text = db.Column(db.Text, default="")
    done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


with app.app_context():
    db.create_all()
    # The user table already exists on Render, so create_all will not add
    # new columns to it. Add them directly; each statement is safe to rerun.
    for ddl in (
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS default_privacy VARCHAR(16) DEFAULT 'public'",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS model_pref VARCHAR(16) DEFAULT 'quality'",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS retention_days INTEGER DEFAULT 3",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS post_time VARCHAR(8) DEFAULT '18:00'",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS posts_per_day INTEGER DEFAULT 1",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS schedule_json TEXT",
        "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS active_channel_id INTEGER",
        "ALTER TABLE channel ADD COLUMN IF NOT EXISTS niche TEXT DEFAULT ''",
        "ALTER TABLE channel ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE channel ADD COLUMN IF NOT EXISTS schedule_json TEXT",
        "ALTER TABLE post ADD COLUMN IF NOT EXISTS channel_pk INTEGER",
        "ALTER TABLE idea ADD COLUMN IF NOT EXISTS channel_pk INTEGER",
    ):
        try:
            db.session.execute(db.text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()
    # Full wipe requested 2026-08-08: clear all Poster records everywhere,
    # including posted-video history and ideas, exactly once. Accounts,
    # channels, tokens, and app settings survive. YouTube itself is
    # untouched; these are only Poster's own records.
    try:
        if not db.session.get(Meta, "full_wipe_1"):
            db.session.execute(db.text(
                "UPDATE channel SET analysis=NULL, schedule_json=NULL, niche=''"))
            db.session.execute(db.text(
                "UPDATE \"user\" SET analysis=NULL, schedule_json=NULL, niche=''"))
            db.session.execute(db.text("DELETE FROM post"))
            db.session.execute(db.text("DELETE FROM idea"))
            db.session.add(Meta(key="full_wipe_1", value="done"))
            db.session.commit()
    except Exception:
        db.session.rollback()
    # One-time clean slate for the channel split. The old shared store mixed
    # both channels' data together, so copies of it can describe the wrong
    # channel. Clear analysis, niche, and schedule everywhere exactly once;
    # from here on every channel builds and keeps strictly its own.
    try:
        if not db.session.get(Meta, "chan_reset_1"):
            db.session.execute(db.text(
                "UPDATE channel SET analysis=NULL, schedule_json=NULL, niche=''"))
            db.session.execute(db.text(
                "UPDATE \"user\" SET analysis=NULL, schedule_json=NULL, niche=''"))
            db.session.add(Meta(key="chan_reset_1", value="done"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def model_for(u):
    return ("claude-haiku-4-5-20251001"
            if (u.model_pref or "quality") == "fast" else "claude-sonnet-4-6")


# ---------- small helpers ----------

def iso_seconds(dur):
    """PT1H2M3S -> seconds. Returns None if it doesn't parse."""
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", dur or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


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


# Accounts that always have full access (the app's own devs). Add more via
# the OWNER_EMAILS env var on Render, comma-separated.
OWNER_EMAILS = set(e.strip().lower() for e in
                   (os.environ.get("OWNER_EMAILS",
                    "chaosatomicshadow@gmail.com,dinoboy0627@gmail.com"))
                   .split(",") if e.strip())


def access_status(u):
    if (u.email or "").strip().lower() in OWNER_EMAILS:
        return "dev"
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


def mint_access_token(u):
    # Turns the selected channel's refresh token into a fresh access token.
    # STRICT: never borrow another identity's token. A channel without its
    # own token may only fall back to the account token when it belongs to
    # the same Google identity the account signed in with; otherwise the
    # caller gets None and the UI asks to reconnect that channel.
    c = active_channel(u)
    if c and c.refresh_token:
        refresh = c.refresh_token
    elif c and c.google_id and u.google_id and c.google_id == u.google_id:
        refresh = u.refresh_token
    elif c is None:
        refresh = u.refresh_token
    else:
        refresh = None
    if not refresh:
        return None
    try:
        tok = http_json("https://oauth2.googleapis.com/token",
                        data=urllib.parse.urlencode({
                            "client_id": os.environ["GOOGLE_CLIENT_ID"],
                            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                            "refresh_token": refresh,
                            "grant_type": "refresh_token",
                        }).encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        return tok.get("access_token")
    except Exception:
        return None


# ---------- pages ----------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/privacy")
def privacy():
    return send_from_directory("static", "privacy.html")


# ---------- Google / YouTube OAuth ----------

SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/yt-analytics.readonly openid email")


def ensure_channels(u):
    """Every user gets at least one Channel row; older accounts are migrated
    from the token that lives on the user record itself."""
    chans = Channel.query.filter_by(user_id=u.id).order_by(Channel.id).all()
    if not chans and u.refresh_token:
        c = Channel(user_id=u.id, google_id=u.google_id,
                    title=u.channel_title or "", refresh_token=u.refresh_token)
        db.session.add(c)
        db.session.commit()
        chans = [c]
    if chans and not u.active_channel_id:
        u.active_channel_id = chans[0].id
        db.session.commit()
    return chans


def active_channel(u):
    chans = ensure_channels(u)
    if not chans:
        return None
    act = None
    for c in chans:
        if c.id == u.active_channel_id:
            act = c
            break
    if act is None:
        act = chans[0]
    # Account-level copies of this data are from the shared-store days and
    # may describe the wrong channel. Never copy them anywhere; just clear
    # them so nothing can bleed between channels again.
    try:
        if u.analysis or u.schedule_json or u.niche:
            u.analysis = None
            u.schedule_json = None
            u.niche = ""
            db.session.commit()
    except Exception:
        db.session.rollback()
    return act


@app.get("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    session["add_channel"] = bool(request.args.get("add")) and bool(session.get("uid"))
    q = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": APP_URL + "/oauth/callback",
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent select_account",
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
    channel_id = ""
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                       headers={"Authorization": "Bearer " + access})
        items = ch.get("items") or []
        if items:
            channel_title = items[0]["snippet"]["title"]
            channel_id = items[0].get("id") or ""
    except Exception:
        pass

    # Adding a second channel to an account that is already signed in
    if session.pop("add_channel", False) and session.get("uid"):
        owner = db.session.get(User, session["uid"])
        if owner:
            c = Channel.query.filter_by(user_id=owner.id, google_id=gid).first()
            if not c and channel_id:
                c = Channel.query.filter_by(user_id=owner.id, channel_id=channel_id).first()
            if not c:
                c = Channel(user_id=owner.id)
                db.session.add(c)
            c.google_id = gid
            c.channel_id = channel_id or c.channel_id
            c.title = channel_title or c.title
            if refresh:
                c.refresh_token = refresh
            db.session.commit()
            owner.active_channel_id = c.id
            db.session.commit()
            return redirect("/")

    u = User.query.filter_by(google_id=gid).first()
    if not u:
        # This Google identity may already be linked as a channel on an
        # existing Poster account; if so, sign into that account instead of
        # creating a fresh empty one.
        linked = Channel.query.filter_by(google_id=gid).order_by(Channel.id).first()
        if linked:
            owner = db.session.get(User, linked.user_id)
            if owner:
                if refresh:
                    linked.refresh_token = refresh
                linked.channel_id = channel_id or linked.channel_id
                linked.title = channel_title or linked.title
                owner.active_channel_id = linked.id
                db.session.commit()
                session["uid"] = owner.id
                session.permanent = True
                return redirect("/")
    if not u:
        u = User(google_id=gid,
                 trial_ends=datetime.datetime.utcnow() + datetime.timedelta(days=TRIAL_DAYS))
        db.session.add(u)
    u.email = info.get("email") or u.email
    u.channel_title = channel_title or u.channel_title
    if refresh:
        u.refresh_token = refresh
    db.session.commit()
    # Keep this sign-in's own channel row current too
    c = Channel.query.filter_by(user_id=u.id, google_id=gid).first()
    if not c:
        c = Channel(user_id=u.id, google_id=gid)
        db.session.add(c)
    c.channel_id = channel_id or c.channel_id
    c.title = channel_title or c.title
    if refresh:
        c.refresh_token = refresh
    db.session.commit()
    if not u.active_channel_id:
        u.active_channel_id = c.id
        db.session.commit()
    session["uid"] = u.id
    session.permanent = True
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


def parse_json_col(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


WEEK_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"signedIn": False})
    status = access_status(u)
    days_left = 0
    if u.trial_ends:
        days_left = max(0, (u.trial_ends - datetime.datetime.utcnow()).days)
    chans = ensure_channels(u)
    act = active_channel(u)
    return jsonify({
        "signedIn": True,
        "email": u.email,
        "channel": (act.title if act and act.title else u.channel_title),
        "channels": [{"id": c.id, "title": c.title or "Untitled channel",
                      "active": bool(act and c.id == act.id)} for c in chans],
        "niche": (act.niche if act else "") or "",
        "status": status,
        "trialDaysLeft": days_left,
        "billingEnabled": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "settings": {
            "privacy": u.default_privacy or "public",
            "model": u.model_pref or "quality",
            "retention": u.retention_days if u.retention_days is not None else 3,
            "postTime": u.post_time or "18:00",
            "postsPerDay": u.posts_per_day or 1,
            "schedule": parse_json_col(act.schedule_json) if act else None,
        },
        "analysis": (lambda a: sanitize_analysis(a) if a else a)(
            parse_json_col(act.analysis) if act else None),
    })


@app.post("/api/channels")
def switch_channel():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    cid = (request.get_json(silent=True) or {}).get("id")
    c = Channel.query.filter_by(user_id=u.id, id=cid).first()
    if not c:
        return jsonify({"ok": False, "error": "no such channel"}), 404
    u.active_channel_id = c.id
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/niche")
def save_niche():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    c = active_channel(u)
    if not c:
        return jsonify({"ok": False, "error": "no channel"}), 409
    c.niche = (request.get_json(silent=True) or {}).get("niche", "")[:2000]
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/niche/auto")
def auto_niche():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    c = active_channel(u)
    if not c:
        return jsonify({"ok": False, "error": "no channel"}), 409
    token = mint_access_token(u)
    if not token:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    H = {"Authorization": "Bearer " + token}
    facts = []
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels"
                       "?part=snippet,statistics&mine=true", headers=H)
        items = ch.get("items") or []
        if items:
            sn = items[0].get("snippet", {})
            st = items[0].get("statistics", {})
            if sn.get("title"):
                facts.append("Channel name: " + sn["title"])
            if sn.get("description"):
                facts.append("Channel description: " + sn["description"][:500])
            if st.get("subscriberCount"):
                facts.append("Subscribers: " + str(st["subscriberCount"]))
    except Exception:
        pass
    try:
        ch2 = http_json("https://www.googleapis.com/youtube/v3/channels"
                        "?part=contentDetails&mine=true", headers=H)
        it2 = ch2.get("items") or []
        uploads = (it2[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                   if it2 else None)
        if uploads:
            pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems"
                           "?part=contentDetails&maxResults=20&playlistId="
                           + urllib.parse.quote(uploads), headers=H)
            ids = [i["contentDetails"]["videoId"]
                   for i in (pl.get("items") or []) if i.get("contentDetails")]
            if ids:
                vids = http_json("https://www.googleapis.com/youtube/v3/videos"
                                 "?part=snippet,contentDetails&id=" + ",".join(ids[:20]),
                                 headers=H)
                titles, tags, shorts = [], set(), 0
                for v in (vids.get("items") or []):
                    vsn = v.get("snippet", {})
                    if vsn.get("title"):
                        titles.append(vsn["title"][:80])
                    for t in (vsn.get("tags") or [])[:6]:
                        tags.add(t)
                    secs = iso_seconds((v.get("contentDetails") or {}).get("duration", ""))
                    if secs is not None and secs < 180:
                        shorts += 1
                if titles:
                    facts.append("Recent video titles: " + " | ".join(titles))
                if tags:
                    facts.append("Common tags: " + ", ".join(list(tags)[:20]))
                facts.append(("Most uploads are Shorts." if shorts > len(titles) / 2
                              else "Mix of Shorts and longer videos."))
    except Exception:
        pass
    if not facts:
        return jsonify({"ok": False, "error": "could not read your channel"}), 502
    prompt = ("Here is what a YouTube channel actually publishes:\n\n"
              + "\n".join(facts)
              + "\n\nWrite a short niche description for this channel in the creator's own"
                " voice, 1 to 2 plain sentences, naming what they make and the vibe. No"
                " preamble, no quotes, no markdown. Just the description.")
    try:
        resp = http_json("https://api.anthropic.com/v1/messages",
                         data={"model": model_for(u),
                               "max_tokens": 200,
                               "messages": [{"role": "user", "content": prompt}]},
                         headers={"Content-Type": "application/json",
                                  "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", [])).strip().strip('"')
        if not text:
            raise ValueError("empty")
    except Exception:
        return jsonify({"ok": False, "error": "could not write a niche, try again"}), 502
    c.niche = text[:2000]
    db.session.commit()
    return jsonify({"ok": True, "niche": c.niche})


@app.post("/api/settings")
def save_settings():
    u = current_user()
    if not u:
        return jsonify({"ok": False, "error": "signed out"}), 401
    c = active_channel(u)
    d = request.get_json(silent=True) or {}
    if "niche" in d:
        if c:
            c.niche = str(d.get("niche", ""))[:2000]
    priv = d.get("privacy")
    if priv in ("public", "unlisted", "private"):
        u.default_privacy = priv
    model = d.get("model")
    if model in ("quality", "fast"):
        u.model_pref = model
    try:
        ret = int(d.get("retention"))
        if ret in (0, 1, 2, 3, 7, 14, 30):
            u.retention_days = ret
    except Exception:
        pass
    pt = str(d.get("postTime", ""))
    if len(pt) == 5 and pt[2] == ":" and pt[:2].isdigit() and pt[3:].isdigit():
        u.post_time = pt
    try:
        ppd = int(d.get("postsPerDay"))
        if 1 <= ppd <= 5:
            u.posts_per_day = ppd
    except Exception:
        pass
    if isinstance(d.get("schedule"), dict):
        raw = d["schedule"]

        def clean_week(w):
            clean = {}
            for day in WEEK_DAYS:
                times = (w or {}).get(day) or []
                keep = []
                for t in times[:8]:
                    t = str(t)
                    if (len(t) == 5 and t[2] == ":" and t[:2].isdigit()
                            and t[3:].isdigit() and int(t[:2]) < 24 and int(t[3:]) < 60
                            and t not in keep):
                        keep.append(t)
                clean[day] = sorted(keep)
            return clean

        if isinstance(raw.get("shorts"), dict) or isinstance(raw.get("videos"), dict):
            new_sched = json.dumps({"shorts": clean_week(raw.get("shorts")),
                                    "videos": clean_week(raw.get("videos"))})
            if c:
                c.schedule_json = new_sched
        else:
            # Old flat week from before the split: treat it as normal videos.
            new_sched = json.dumps({"shorts": clean_week(None),
                                    "videos": clean_week(raw)})
            if c:
                c.schedule_json = new_sched
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True})


@app.post("/api/youtube-token")
def youtube_token():
    # Fresh access token so the browser can upload the video straight to
    # YouTube. The video file never touches this server.
    u, err = require_access()
    if err:
        return err
    token = mint_access_token(u)
    if not token:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    return jsonify({"ok": True, "accessToken": token, "expiresIn": 3600})


# ---------- channel + trending context for generation ----------

def youtube_context(u):
    # Pulls the user's recent video performance and what is trending right
    # now, and folds both into a short text block for the prompt. Best
    # effort: any failure just returns whatever was gathered so far.
    token = mint_access_token(u)
    if not token:
        return ""
    H = {"Authorization": "Bearer " + token}
    lines = []
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels"
                       "?part=snippet,contentDetails,statistics&mine=true", headers=H)
        items = ch.get("items") or []
        if items:
            try:
                act0 = active_channel(u)
                sn0 = items[0].get("snippet", {})
                if act0 and sn0.get("title"):
                    act0.title = sn0["title"]
                    act0.channel_id = items[0].get("id") or act0.channel_id
                    db.session.commit()
            except Exception:
                db.session.rollback()
            stats = items[0].get("statistics", {})
            subs = stats.get("subscriberCount")
            if subs:
                lines.append("Channel subscribers: " + str(subs))
            uploads = (items[0].get("contentDetails", {})
                       .get("relatedPlaylists", {}).get("uploads"))
            if uploads:
                pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems"
                               "?part=contentDetails&maxResults=5&playlistId="
                               + urllib.parse.quote(uploads), headers=H)
                ids = [i["contentDetails"]["videoId"]
                       for i in (pl.get("items") or []) if i.get("contentDetails")]
                if ids:
                    vids = http_json("https://www.googleapis.com/youtube/v3/videos"
                                     "?part=snippet,statistics&id=" + ",".join(ids),
                                     headers=H)
                    recent = []
                    for v in (vids.get("items") or []):
                        s = v.get("statistics", {})
                        recent.append('"' + v["snippet"]["title"][:70] + '" '
                                      + str(s.get("viewCount", "?")) + " views, "
                                      + str(s.get("likeCount", "?")) + " likes")
                    if recent:
                        lines.append("Creator's recent uploads and how they did: "
                                     + "; ".join(recent))
    except Exception:
        pass
    try:
        tr = http_json("https://www.googleapis.com/youtube/v3/videos"
                       "?part=snippet&chart=mostPopular&regionCode=US&maxResults=8",
                       headers=H)
        titles = []
        tags = []
        for v in (tr.get("items") or []):
            sn = v.get("snippet", {})
            if sn.get("title"):
                titles.append(sn["title"][:60])
            for t in (sn.get("tags") or [])[:4]:
                if t.lower() not in tags:
                    tags.append(t.lower())
        if titles:
            lines.append("Trending on YouTube right now: " + "; ".join(titles[:8]))
        if tags:
            lines.append("Tags trending videos are using: " + ", ".join(tags[:20]))
    except Exception:
        pass
    return "\n".join(lines)


# ---------- posted history ----------

@app.get("/api/posts")
def list_posts():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    c = active_channel(u)
    q = Post.query.filter_by(user_id=u.id)
    if c:
        # Legacy rows (no channel recorded) stay visible on the first channel.
        q = q.filter(db.or_(Post.channel_pk == c.id, Post.channel_pk.is_(None)))
    rows = q.order_by(Post.created_at.desc()).limit(300).all()
    return jsonify({"ok": True, "posts": [{
        "id": p.id, "videoId": p.video_id or "", "title": p.title or "",
        "format": p.format or "video", "publishAt": p.publish_at or "",
        "crossed": (p.crossed or "").split(",") if p.crossed else [],
        "createdAt": (p.created_at.isoformat() + "Z") if p.created_at else ""
    } for p in rows]})


@app.post("/api/posts")
def record_post():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    d = request.get_json(silent=True) or {}
    c = active_channel(u)
    p = Post(user_id=u.id,
             channel_pk=(c.id if c else None),
             video_id=str(d.get("videoId", ""))[:32],
             title=str(d.get("title", ""))[:255],
             format="short" if d.get("format") == "short" else "video",
             publish_at=str(d.get("publishAt", ""))[:40])
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "id": p.id})


@app.post("/api/posts/<int:pid>/crossed")
def mark_crossed(pid):
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    p = Post.query.filter_by(id=pid, user_id=u.id).first()
    if not p:
        return jsonify({"ok": False}), 404
    plat = (request.get_json(silent=True) or {}).get("platform", "")
    if plat in ("tiktok", "instagram", "x"):
        cur = set((p.crossed or "").split(",")) - {""}
        cur.add(plat)
        p.crossed = ",".join(sorted(cur))
        db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/posts/<int:pid>/delete")
def delete_post(pid):
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    p = Post.query.filter_by(id=pid, user_id=u.id).first()
    if p:
        db.session.delete(p)
        db.session.commit()
    return jsonify({"ok": True})


# ---------- ideas ----------

@app.get("/api/ideas")
def list_ideas():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    c = active_channel(u)
    q = Idea.query.filter_by(user_id=u.id)
    if c:
        q = q.filter(db.or_(Idea.channel_pk == c.id, Idea.channel_pk.is_(None)))
    rows = q.order_by(Idea.done.asc(), Idea.created_at.desc()).limit(200).all()
    return jsonify({"ok": True, "ideas": [{
        "id": i.id, "text": i.text or "", "done": bool(i.done)
    } for i in rows]})


@app.post("/api/ideas")
def add_idea():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    text = str((request.get_json(silent=True) or {}).get("text", "")).strip()[:500]
    if not text:
        return jsonify({"ok": False, "error": "empty"}), 400
    c = active_channel(u)
    i = Idea(user_id=u.id, channel_pk=(c.id if c else None), text=text)
    db.session.add(i)
    db.session.commit()
    return jsonify({"ok": True, "id": i.id})


@app.post("/api/ideas/<int:iid>/toggle")
def toggle_idea(iid):
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    i = Idea.query.filter_by(id=iid, user_id=u.id).first()
    if not i:
        return jsonify({"ok": False}), 404
    i.done = not i.done
    db.session.commit()
    return jsonify({"ok": True, "done": i.done})


@app.post("/api/ideas/<int:iid>/delete")
def delete_idea(iid):
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    i = Idea.query.filter_by(id=iid, user_id=u.id).first()
    if i:
        db.session.delete(i)
        db.session.commit()
    return jsonify({"ok": True})


IDEAS_PROMPT = """You are a YouTube content strategist. A creator makes videos with AI
generation tools and needs ready-to-paste prompts.

Their niche, in their own words: {niche}

{context}

Write ONE video generation prompt: the single video most likely to do well on
this channel right now, judged from their numbers and what is trending. Make it
detailed and self-contained so the creator can paste it straight into an AI
video tool: the scene, subject, mood, lighting, camera and motion, and how it
should open in the first second. 40 to 80 words. No hedging, commit to one idea.

Respond with ONLY a JSON array containing that one string, no preamble, no
markdown fences."""

ADVICE_PROMPT = """You are a YouTube growth coach. Below is real data from one creator's
channel plus what is trending right now.

Their niche, in their own words: {niche}

{context}

Give calm, curated growth advice. No walls of text, no jargon. Be specific to
this channel's numbers.

Respond with ONLY a JSON object, no preamble, no markdown fences, shaped exactly:
{{
 "headline": "one sentence, the single most important thing to do next",
 "advice": [four objects like {{"title": "3 to 5 word label", "tip": "one or two plain sentences of concrete advice"}}]
}}"""


@app.post("/api/ideas/generate")
def generate_ideas():
    u, err = require_access()
    if err:
        return err
    mode = (request.get_json(silent=True) or {}).get("mode", "prompts")
    context = youtube_context(u)
    c = active_channel(u)
    learned = parse_json_col(c.analysis) if c else None
    if learned and learned.get("whatWorks"):
        context = ((context + "\n\n") if context else "") \
            + "What Poster's own analysis of this channel found is working:\n" \
            + "\n".join("- " + str(w) for w in learned["whatWorks"][:5])
    prompt = (ADVICE_PROMPT if mode == "advice" else IDEAS_PROMPT).format(
        niche=((c.niche if c else "") or "not given"),
        context=context or "No channel data available.")
    try:
        resp = http_json("https://api.anthropic.com/v1/messages",
                         data={"model": model_for(u),
                               "max_tokens": 900,
                               "messages": [{"role": "user", "content": prompt}]},
                         headers={"Content-Type": "application/json",
                                  "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
    except Exception:
        return jsonify({"ok": False, "error": "generation failed, try again"}), 502
    u.generations = (u.generations or 0) + 1
    if mode == "advice":
        db.session.commit()
        return jsonify({"ok": True, "headline": parsed.get("headline", ""),
                        "advice": parsed.get("advice", [])})
    if not isinstance(parsed, list):
        db.session.rollback()
        return jsonify({"ok": False, "error": "generation failed, try again"}), 502
    saved = []
    for t in parsed[:10]:
        i = Idea(user_id=u.id, channel_pk=(c.id if c else None), text=str(t)[:500])
        db.session.add(i)
        saved.append(i)
    db.session.commit()
    return jsonify({"ok": True, "ideas": [{"id": i.id, "text": i.text, "done": False} for i in saved]})


# ---------- schedule view: everything on the channel, pulled live ----------

@app.get("/api/schedule")
def schedule_view():
    # Pulls the channel's upcoming scheduled videos and recent uploads so
    # the app can show everything in one place. Read-only, best effort.
    u, err = require_access()
    if err:
        return err
    token = mint_access_token(u)
    if not token:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    H = {"Authorization": "Bearer " + token}
    out = {"ok": True, "scheduled": [], "recent": []}
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels"
                       "?part=contentDetails&mine=true", headers=H)
        items = ch.get("items") or []
        uploads = (items[0].get("contentDetails", {})
                   .get("relatedPlaylists", {}).get("uploads")) if items else None
        if not uploads:
            return jsonify(out)
        pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems"
                       "?part=contentDetails&maxResults=20&playlistId="
                       + urllib.parse.quote(uploads), headers=H)
        ids = [i["contentDetails"]["videoId"]
               for i in (pl.get("items") or []) if i.get("contentDetails")]
        if not ids:
            return jsonify(out)
        vids = http_json("https://www.googleapis.com/youtube/v3/videos"
                         "?part=snippet,status,statistics,contentDetails&id=" + ",".join(ids[:20]),
                         headers=H)
        now = datetime.datetime.utcnow().isoformat() + "Z"
        for v in (vids.get("items") or []):
            sn = v.get("snippet", {})
            st = v.get("status", {})
            stats = v.get("statistics", {})
            entry = {
                "id": v.get("id"),
                "title": sn.get("title", ""),
                "publishedAt": sn.get("publishedAt", ""),
                "views": stats.get("viewCount", "0"),
                "likes": stats.get("likeCount", "0"),
                "comments": stats.get("commentCount", "0"),
                "desc": (sn.get("description") or "")[:300],
                "duration": (v.get("contentDetails") or {}).get("duration", ""),
                "privacy": st.get("privacyStatus", ""),
            }
            publish_at = st.get("publishAt")
            if st.get("privacyStatus") == "private" and publish_at and publish_at > now:
                entry["publishAt"] = publish_at
                out["scheduled"].append(entry)
            elif st.get("privacyStatus") == "public":
                out["recent"].append(entry)
        out["scheduled"].sort(key=lambda e: e.get("publishAt", ""))
        out["recent"].sort(key=lambda e: e.get("publishedAt", ""), reverse=True)
        out["recent"] = out["recent"][:10]
    except Exception:
        pass
    return jsonify(out)


# ---------- Claude metadata generation ----------

CATEGORIES = ["horror", "story", "funny", "gaming", "satisfying", "educational", "other"]

GEN_PROMPT = """You are writing the listing copy for one short-form video.
The still frames above were sampled from the video in order. Describe only what
is visibly in the frames; if they are dark or ambiguous, stay safe rather than
inventing a story beat you cannot see.

Creator's niche: {niche}
Original file name: {filename}
Format: this will be posted as a YouTube {format}. If it is a Short, keep the
title punchy and under 60 characters and write for a feed viewer; if it is a
regular video, the title can breathe a little more.

{context}

Where the channel and trending context above is useful, let it steer the wording:
lean toward phrasing and tags similar to what performed well for this creator and
what is trending now, but never claim things the frames do not show.

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
    fmt = data.get("format")
    if fmt not in ("short", "video"):
        fmt = "video"
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

    ctx = youtube_context(u)
    c = active_channel(u)
    learned = parse_json_col(c.analysis) if c else None
    if learned:
        extra = []
        if learned.get("whatWorks"):
            extra.append("What Poster's own analysis of this channel's real numbers found is working:")
            extra.extend("- " + str(w) for w in learned["whatWorks"][:5])
        if learned.get("summary"):
            extra.append("Channel state: " + str(learned["summary"]))
        if extra:
            ctx = (ctx + "\n\n" if ctx else "") + "\n".join(extra)
    content.append({"type": "text",
                    "text": GEN_PROMPT.format(niche=((c.niche if c else "") or "general short-form content"),
                                              filename=filename,
                                              format=("Short" if fmt == "short" else "video"),
                                              context=(ctx or "No channel or trending context available."),
                                              cats=json.dumps(CATEGORIES))})
    try:
        resp = http_json("https://api.anthropic.com/v1/messages",
                         data={"model": model_for(u),
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


# ---------- channel analysis ----------

ANALYZE_PROMPT = """You are a YouTube channel strategist. Below is real data pulled
from one creator's channel plus what is trending on YouTube right now.

Creator's own description of their niche: {niche}

{data}
{previous}
Study how their videos performed BY DAY OF THE WEEK and by hour. Every publish
time in the data below is already in US Eastern time, the creator's own
timezone, so treat the hours as-is. Days are not
interchangeable: look at which weekdays their better videos went out and at what
Eastern hour, and build a posting week from that evidence. Days can differ from
each other and days can be empty. Where a day has no evidence, either leave it
empty or borrow from the nearest similar day.

Every upload is labeled [SHORT] (under 3 minutes) or [VIDEO] (a normal long
video). Treat them as two different products with different cadences: Shorts
can go out as often as daily or more, normal videos usually far less often,
sometimes just once or twice a week. Build a SEPARATE posting week for each
format from that format's own evidence only. If the channel has no uploads of
one format, leave that format's week empty rather than inventing times for it.

Any line marked MEASURED comes straight from the YouTube Analytics API. That
is ground truth: build weekSchedule primarily from the measured weekday
averages when they are present, and use the measured watch percentages to
judge which videos actually hold attention.

Guard against overfitting. One or two videos on a weekday is weak evidence:
a single flop on a Tuesday does not make Tuesday a bad day, and a single hit
does not make it a great one. Only claim a day-level pattern when at least
three videos on that day point the same way; otherwise fall back to the
channel's overall best hours and say the evidence for that day is thin. Views
also depend on the video itself, so compare each video against the channel's
typical numbers before crediting or blaming the time slot. Be concrete and
honest, keep confidence modest, and never pad with generic advice that would
fit any channel.

Respond with ONLY a JSON object, no preamble, no markdown fences, shaped exactly:
{{
 "summary": "4 to 6 sentences on the state of the channel and its biggest opportunity right now, naming real numbers from the data (views, watch percentages, subscriber count) rather than vague trends. If a previous analysis is shown above, do not restate it; lead with what is different since then.",
 "sinceLast": ["2 to 4 short notes on what actually changed since the previous analysis: views that moved, a video that took off or flopped, whether earlier advice shows up in the new uploads. Empty array if there was no previous analysis."],
 "whatWorks": ["4 to 6 observations about what is performing and why, each one or two full sentences naming the actual videos and numbers behind it, e.g. which upload pulled how many views or held what watch percentage. Favor observations the previous analysis did not already make."],
 "shorts": {{"weekSchedule": {{"mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []}}, "weekWhy": []}},
 "videos": {{"weekSchedule": {{"mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []}}, "weekWhy": []}},
 where shorts covers only the [SHORT] uploads and videos covers only the [VIDEO] uploads. In each weekSchedule every day is a list of 0 or more 24-hour "HH:MM" times in US Eastern time, derived only from the actual Eastern hours that format's better uploads went out, shown above. NEVER output 00:00 or any hour you did not derive from real data; if you do not have a real hour for a day, either leave that day empty or reuse a proven hour from a similar day. Put more times on days the measured numbers prove are strong, fewer or none on weak days. Shorts may earn several days a week; normal videos should usually get only one to three slots a week unless the evidence clearly supports more. Each weekWhy is one short line per day that format posts on, naming the day, how it performs, and why you chose it, e.g. 'Friday: your best day, top three Shorts all went out Friday evening and pulled well above your average'. Empty array if no day-level evidence,
 "bestTimes": ["up to 3 of the strongest windows as human strings, e.g. 'Fridays around 6 PM'"],
 "bestHours": [up to three integers 0 to 23 matching those windows],
 "postsPerDay": one integer 1 to 5, how many videos per day this channel can post without views per video collapsing, judged from its niche, current performance, and how many videos are waiting in the creator's queue,
 "contentIdeas": ["5 to 8 specific video ideas that fit this channel and current trends, each one sentence with a short reason why it fits, none repeated from the previous analysis"]
}}"""


def _clean_hhmm(t):
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(t or ""))
    if not m:
        return None
    hhmm = m.group(1).zfill(2) + ":" + m.group(2)
    if hhmm == "00:00":
        return None
    return hhmm


def _to12(hhmm):
    h, mm = int(hhmm[:2]), hhmm[3:]
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return str(h12) + ("" if mm == "00" else ":" + mm) + " " + ap


DAY_KEYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
            "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def _sanitize_week_block(out):
    """Rebuild one weekSchedule from clean times and rewrite its weekWhy
    lines so their times match, dropping lines for any day left empty."""
    week = out.get("weekSchedule") or {}
    clean_week = {}
    for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        seen, keep = set(), []
        for t in (week.get(k) or []):
            c = _clean_hhmm(t)
            if c and c not in seen:
                seen.add(c)
                keep.append(c)
        keep.sort()
        clean_week[k] = keep
    out["weekSchedule"] = clean_week

    good_days = {DAY_KEYS[k]: v for k, v in clean_week.items() if v}
    why_in = out.get("weekWhy") or []
    why_out = []
    for line in why_in:
        s = str(line)
        head = s.split(":", 1)[0].strip()
        day = None
        for name in good_days:
            if name.lower() in head.lower() or name[:3].lower() in head.lower():
                day = name
                break
        if not day:
            continue  # weekWhy line for a day with no valid times -> drop it
        times12 = ", ".join(_to12(t) for t in good_days[day])
        rest = s.split(":", 1)[1].strip() if ":" in s else s
        # The model's own sentence may name made-up times; strip every time
        # mention so the only times shown are the verified ones we append.
        T = r"\d{1,2}(?::\d{2})?\s*(?:A\.?M\.?|P\.?M\.?)|\d{1,2}:\d{2}"
        phrase = (r"(?:\b(?:post(?:ing|ed)?|upload(?:ing|ed)?|go(?:es|ing)? live)\s+)?"
                  r"(?:\b(?:at|around|by|near)\s+)?"
                  r"(?:" + T + r")(?:\s*(?:and|or|to|,|-|\u2013)\s*(?:" + T + r"))*")
        rest = re.sub(phrase, "", rest, flags=re.I)
        rest = re.sub(r"\s{2,}", " ", rest)
        rest = re.sub(r"\s+([,.;])", r"\1", rest)
        rest = re.sub(r"(?:,\s*)+,", ",", rest)
        rest = re.sub(r",\s*(?=[.;)]|$)", "", rest).strip(" ,;.")
        why_out.append(day + ": " + (rest + " " if rest else "") + "(posting " + times12 + ")")
    out["weekWhy"] = why_out
    return out


def sanitize_analysis(out):
    """Never let a time reach the UI that is not a valid, non-midnight slot
    the schedule actually contains. Cleans the Shorts week and the normal
    videos week separately, plus the old flat shape from earlier saves,
    then scrubs bestTimes/bestHours."""
    for tr in ("shorts", "videos"):
        blk = out.get(tr)
        if isinstance(blk, dict):
            _sanitize_week_block(blk)
    if isinstance(out.get("weekSchedule"), dict) or out.get("weekWhy"):
        _sanitize_week_block(out)

    bad_time = re.compile(r"\b12\s*A\.?M\.?\b|\b(1[3-9]|2[0-9])\s*[AP]\.?M\.?\b|\b00:00\b", re.I)
    out["bestTimes"] = [str(t) for t in (out.get("bestTimes") or [])
                        if not bad_time.search(str(t))][:3]

    bh = []
    for h in (out.get("bestHours") or []):
        try:
            hi = int(h)
        except Exception:
            continue
        if 0 <= hi <= 23 and hi != 0 and hi not in bh:
            bh.append(hi)
    out["bestHours"] = bh[:3]
    return out


@app.post("/api/analyze")
def analyze():
    u, err = require_access()
    if err:
        return err
    token = mint_access_token(u)
    if not token:
        act = active_channel(u)
        who = (' for "' + act.title + '"') if act and act.title else ""
        return jsonify({"ok": False,
                        "error": "this channel is not fully connected" + who
                                 + ". Use Add channel to reconnect it, then try again."}), 409
    H = {"Authorization": "Bearer " + token}
    lines = []
    recent_ids = []
    title_by_id = {}
    fmt_by_id = {}
    try:
        qc = int((request.get_json(silent=True) or {}).get("queueCount") or 0)
        if qc > 0:
            lines.append("The creator currently has " + str(min(qc, 500))
                         + " unposted videos waiting in their Poster queue.")
    except Exception:
        pass
    try:
        ch = http_json("https://www.googleapis.com/youtube/v3/channels"
                       "?part=contentDetails,statistics&mine=true", headers=H)
        items = ch.get("items") or []
        if items:
            stats = items[0].get("statistics", {})
            lines.append("Channel totals: " + str(stats.get("subscriberCount", "?"))
                         + " subscribers, " + str(stats.get("videoCount", "?"))
                         + " videos, " + str(stats.get("viewCount", "?")) + " lifetime views.")
            uploads = (items[0].get("contentDetails", {})
                       .get("relatedPlaylists", {}).get("uploads"))
            if uploads:
                pl = http_json("https://www.googleapis.com/youtube/v3/playlistItems"
                               "?part=contentDetails&maxResults=25&playlistId="
                               + urllib.parse.quote(uploads), headers=H)
                ids = [i["contentDetails"]["videoId"]
                       for i in (pl.get("items") or []) if i.get("contentDetails")]
                if ids:
                    recent_ids = ids
                    vids = http_json("https://www.googleapis.com/youtube/v3/videos"
                                     "?part=snippet,contentDetails,statistics&id=" + ",".join(ids),
                                     headers=H)
                    lines.append("Recent uploads, newest first (format, title, day of week, published time US Eastern, views, likes, comments). SHORT means under 3 minutes, VIDEO means a normal long video:")
                    for v in (vids.get("items") or []):
                        sn = v.get("snippet", {})
                        s = v.get("statistics", {})
                        title_by_id[v.get("id") or ""] = sn.get("title", "")[:60]
                        secs = iso_seconds((v.get("contentDetails") or {}).get("duration", ""))
                        fmt = "SHORT" if (secs and secs < 180) else "VIDEO"
                        fmt_by_id[v.get("id") or ""] = fmt
                        pub = sn.get("publishedAt", "")
                        dow, hhmm = "?", "?"
                        try:
                            dt = (datetime.datetime
                                  .fromisoformat(pub.replace("Z", "+00:00"))
                                  .astimezone(APP_TZ))
                            dow = dt.strftime("%A")
                            hhmm = dt.strftime("%H:%M") + " Eastern"
                        except Exception:
                            pass
                        lines.append('- [' + fmt + '] "' + sn.get("title", "")[:80] + '" | went out '
                                     + dow + " at " + hhmm + " | "
                                     + str(s.get("viewCount", "?")) + " views | "
                                     + str(s.get("likeCount", "?")) + " likes | "
                                     + str(s.get("commentCount", "?")) + " comments")
    except Exception:
        pass
    try:
        tr = http_json("https://www.googleapis.com/youtube/v3/videos"
                       "?part=snippet&chart=mostPopular&regionCode=US&maxResults=8",
                       headers=H)
        titles = [v["snippet"]["title"][:60] for v in (tr.get("items") or [])
                  if v.get("snippet", {}).get("title")]
        if titles:
            lines.append("Trending on YouTube right now: " + "; ".join(titles))
    except Exception:
        pass
    # Real measured numbers from the YouTube Analytics API. These make the
    # per-day advice factual instead of inferred from a handful of uploads.
    analytics_ok = False
    try:
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=90)).isoformat()
        rep = http_json("https://youtubeanalytics.googleapis.com/v2/reports"
                        "?ids=channel%3D%3DMINE&startDate=" + start
                        + "&endDate=" + today.isoformat()
                        + "&metrics=views,estimatedMinutesWatched"
                        + "&dimensions=day&sort=day", headers=H)
        rows = rep.get("rows") or []
        if rows:
            analytics_ok = True
            byday = {}
            for r in rows:
                try:
                    d = datetime.date.fromisoformat(str(r[0]))
                    byday.setdefault(d.strftime("%A"), []).append(int(r[1] or 0))
                except Exception:
                    pass
            parts = []
            for name in ("Monday", "Tuesday", "Wednesday", "Thursday",
                         "Friday", "Saturday", "Sunday"):
                vs = byday.get(name) or []
                if vs:
                    parts.append(name + " avg " + str(round(sum(vs) / len(vs)))
                                 + " views/day across " + str(len(vs)) + " days")
            if parts:
                lines.append("MEASURED channel views by weekday, last 90 days,"
                             " straight from the YouTube Analytics API: "
                             + "; ".join(parts))
    except Exception:
        pass
    try:
        if recent_ids:
            today = datetime.date.today()
            start = (today - datetime.timedelta(days=90)).isoformat()
            rep = http_json("https://youtubeanalytics.googleapis.com/v2/reports"
                            "?ids=channel%3D%3DMINE&startDate=" + start
                            + "&endDate=" + today.isoformat()
                            + "&metrics=views,averageViewDuration,averageViewPercentage"
                            + "&dimensions=video&sort=-views&maxResults=25"
                            + "&filters=" + urllib.parse.quote(
                                "video==" + ",".join(recent_ids[:25])), headers=H)
            vrows = rep.get("rows") or []
            if vrows:
                analytics_ok = True
                lines.append("MEASURED per-video watch data, last 90 days"
                             " (how long people actually watched):")
                for r in vrows[:25]:
                    vid = str(r[0])
                    lines.append('- [' + (fmt_by_id.get(vid) or "VIDEO") + '] "'
                                 + (title_by_id.get(vid) or vid) + '" | '
                                 + str(r[1]) + " views | viewers watched "
                                 + str(round(float(r[3] or 0))) + "% on average ("
                                 + str(round(float(r[2] or 0))) + " seconds)")
    except Exception:
        pass

    if not lines:
        return jsonify({"ok": False, "error": "could not read your channel"}), 502

    act = active_channel(u)
    previous = ""
    prev = parse_json_col(act.analysis) if act else None
    if prev:
        keep = {k: prev[k] for k in
                ("savedAt", "summary", "whatWorks", "bestTimes", "contentIdeas")
                if k in prev}
        previous = ("\nYour PREVIOUS analysis of this same channel"
                    + (", saved " + str(prev.get("savedAt")) if prev.get("savedAt") else "")
                    + ":\n" + json.dumps(keep)
                    + "\nDo not repeat any of it back. Compare the new numbers"
                    " against it and report what moved.\n")

    try:
        resp = http_json("https://api.anthropic.com/v1/messages",
                         data={"model": model_for(u),
                               "max_tokens": 4000,
                               "messages": [{"role": "user",
                                             "content": ANALYZE_PROMPT.format(
                                                 niche=((act.niche if act else "") or "not given"),
                                                 data="\n".join(lines),
                                                 previous=previous)}]},
                         headers={"Content-Type": "application/json",
                                  "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        # The model sometimes wraps or trails the JSON; keep just the object.
        if "{" in text and "}" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
        out = json.loads(text)
        out["analytics"] = analytics_ok
        out = sanitize_analysis(out)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": "the write-up came back malformed ("
                                 + type(e).__name__ + "), hit Refresh to retry"}), 502

    u.generations = (u.generations or 0) + 1
    try:
        saved = dict(out)
        saved["savedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
        if act:
            act.analysis = json.dumps(saved)
    except Exception:
        pass
    db.session.commit()
    out["ok"] = True
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
