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
    default_privacy = db.Column(db.String(16), default="public")
    model_pref = db.Column(db.String(16), default="quality")
    retention_days = db.Column(db.Integer, default=3)   # 0 means forever
    post_time = db.Column(db.String(8), default="18:00")
    posts_per_day = db.Column(db.Integer, default=1)
    analysis = db.Column(db.Text)        # last channel analysis JSON; fed back into generation
    schedule_json = db.Column(db.Text)   # weekly posting schedule {"mon":["18:00"],...}


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    video_id = db.Column(db.String(32))
    title = db.Column(db.String(255), default="")
    format = db.Column(db.String(16), default="video")
    publish_at = db.Column(db.String(40))   # ISO string if scheduled, else empty
    crossed = db.Column(db.String(64), default="")  # comma list: tiktok,instagram,x
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Idea(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
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
    ):
        try:
            db.session.execute(db.text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()


def model_for(u):
    return ("claude-haiku-4-5-20251001"
            if (u.model_pref or "quality") == "fast" else "claude-sonnet-4-6")


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


def mint_access_token(u):
    # Turns the stored refresh token into a fresh access token.
    if not u.refresh_token:
        return None
    try:
        tok = http_json("https://oauth2.googleapis.com/token",
                        data=urllib.parse.urlencode({
                            "client_id": os.environ["GOOGLE_CLIENT_ID"],
                            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                            "refresh_token": u.refresh_token,
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
    return jsonify({
        "signedIn": True,
        "email": u.email,
        "channel": u.channel_title,
        "niche": u.niche or "",
        "status": status,
        "trialDaysLeft": days_left,
        "billingEnabled": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "settings": {
            "privacy": u.default_privacy or "public",
            "model": u.model_pref or "quality",
            "retention": u.retention_days if u.retention_days is not None else 3,
            "postTime": u.post_time or "18:00",
            "postsPerDay": u.posts_per_day or 1,
            "schedule": parse_json_col(u.schedule_json),
        },
        "analysis": parse_json_col(u.analysis),
    })


@app.post("/api/niche")
def save_niche():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    u.niche = (request.get_json(silent=True) or {}).get("niche", "")[:2000]
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/settings")
def save_settings():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    d = request.get_json(silent=True) or {}
    if "niche" in d:
        u.niche = str(d.get("niche", ""))[:2000]
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
        clean = {}
        for day in WEEK_DAYS:
            times = d["schedule"].get(day) or []
            keep = []
            for t in times[:8]:
                t = str(t)
                if (len(t) == 5 and t[2] == ":" and t[:2].isdigit()
                        and t[3:].isdigit() and int(t[:2]) < 24 and int(t[3:]) < 60
                        and t not in keep):
                    keep.append(t)
            clean[day] = sorted(keep)
        u.schedule_json = json.dumps(clean)
    db.session.commit()
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
                       "?part=contentDetails,statistics&mine=true", headers=H)
        items = ch.get("items") or []
        if items:
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
    rows = (Post.query.filter_by(user_id=u.id)
            .order_by(Post.created_at.desc()).limit(300).all())
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
    p = Post(user_id=u.id,
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
    rows = (Idea.query.filter_by(user_id=u.id)
            .order_by(Idea.done.asc(), Idea.created_at.desc()).limit(200).all())
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
    i = Idea(user_id=u.id, text=text)
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
    learned = parse_json_col(u.analysis)
    if learned and learned.get("whatWorks"):
        context = ((context + "\n\n") if context else "") \
            + "What Poster's own analysis of this channel found is working:\n" \
            + "\n".join("- " + str(w) for w in learned["whatWorks"][:5])
    prompt = (ADVICE_PROMPT if mode == "advice" else IDEAS_PROMPT).format(
        niche=(u.niche or "not given"),
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
        i = Idea(user_id=u.id, text=str(t)[:500])
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
                         "?part=snippet,status,statistics&id=" + ",".join(ids[:20]),
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
    learned = parse_json_col(u.analysis)
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
                    "text": GEN_PROMPT.format(niche=(u.niche or "general short-form content"),
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
Study how their videos performed BY DAY OF THE WEEK and by hour. Days are not
interchangeable: look at which weekdays their better videos went out and at what
local hour, and build a posting week from that evidence. Days can differ from
each other and days can be empty. Where a day has no evidence, either leave it
empty or borrow from the nearest similar day. Be concrete and honest; if the
data is thin say so and keep confidence modest. Never pad with generic advice
that would fit any channel.

Respond with ONLY a JSON object, no preamble, no markdown fences, shaped exactly:
{{
 "summary": "2 or 3 sentences on the state of the channel and its biggest opportunity right now. If a previous analysis is shown above, do not restate it; lead with what is different since then.",
 "sinceLast": ["2 to 4 short notes on what actually changed since the previous analysis: views that moved, a video that took off or flopped, whether earlier advice shows up in the new uploads. Empty array if there was no previous analysis."],
 "whatWorks": ["3 to 5 short observations about what is performing and why. Favor observations the previous analysis did not already make."],
 "weekSchedule": {{"mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []}} where each day is a list of 0 or more 24-hour "HH:MM" times drawn from that day's own evidence in the creator's likely local time; put more times on proven days, fewer or none on weak days, and cap the total across the week at postsPerDay times 7,
 "bestTimes": ["up to 3 of the strongest windows as human strings, e.g. 'Fridays around 6 PM'"],
 "bestHours": [up to three integers 0 to 23 matching those windows],
 "postsPerDay": one integer 1 to 5, how many videos per day this channel can post without views per video collapsing, judged from its niche, current performance, and how many videos are waiting in the creator's queue,
 "contentIdeas": ["4 to 6 specific video ideas that fit this channel and current trends, each under 15 words, none repeated from the previous analysis"]
}}"""


@app.post("/api/analyze")
def analyze():
    u, err = require_access()
    if err:
        return err
    token = mint_access_token(u)
    if not token:
        return jsonify({"ok": False, "error": "reconnect YouTube"}), 409
    H = {"Authorization": "Bearer " + token}
    lines = []
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
                    vids = http_json("https://www.googleapis.com/youtube/v3/videos"
                                     "?part=snippet,statistics&id=" + ",".join(ids),
                                     headers=H)
                    lines.append("Recent uploads, newest first (title, day of week, published time UTC, views, likes, comments):")
                    for v in (vids.get("items") or []):
                        sn = v.get("snippet", {})
                        s = v.get("statistics", {})
                        pub = sn.get("publishedAt", "")
                        try:
                            dow = datetime.datetime.fromisoformat(
                                pub.replace("Z", "+00:00")).strftime("%A")
                        except Exception:
                            dow = "?"
                        lines.append('- "' + sn.get("title", "")[:80] + '" | '
                                     + dow + " | " + (pub or "?") + " | "
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
    if not lines:
        return jsonify({"ok": False, "error": "could not read your channel"}), 502

    previous = ""
    prev = parse_json_col(u.analysis)
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
                               "max_tokens": 1600,
                               "messages": [{"role": "user",
                                             "content": ANALYZE_PROMPT.format(
                                                 niche=(u.niche or "not given"),
                                                 data="\n".join(lines),
                                                 previous=previous)}]},
                         headers={"Content-Type": "application/json",
                                  "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                  "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        out = json.loads(text)
    except Exception:
        return jsonify({"ok": False, "error": "analysis failed, try again"}), 502

    u.generations = (u.generations or 0) + 1
    try:
        saved = dict(out)
        saved["savedAt"] = datetime.datetime.utcnow().isoformat() + "Z"
        u.analysis = json.dumps(saved)
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
