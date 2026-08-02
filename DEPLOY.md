# Putting Poster online, step by step

This folder is the public version of Poster. Three files matter:
app.py (the server), static/index.html (the site), requirements.txt (what
Render installs). Your personal version in PosterApp stays as it is; this is
a separate thing.

## 1. Put the code on GitHub

Render deploys from GitHub. Make a free GitHub account if you don't have one,
create a new repository called poster, and upload these files into it
(app.py, requirements.txt, and the static folder with index.html inside).
GitHub's website lets you drag and drop files, no commands needed.

## 2. Create the Render service

1. Go to render.com, sign up with that GitHub account.
2. New > Web Service > pick your poster repository.
3. Settings:
   - Runtime: Python
   - Build command:  pip install -r requirements.txt
   - Start command:  gunicorn app:app
   - Instance type: Free
4. Add environment variables (Environment tab):
   - SECRET_KEY            any long random string, mash the keyboard
   - GOOGLE_CLIENT_ID      from your Google Cloud OAuth client
   - GOOGLE_CLIENT_SECRET  same page
   - ANTHROPIC_API_KEY     your Claude API key
   - APP_URL               your Render URL for now, like https://poster-xxxx.onrender.com
5. Deploy. In a minute you get that onrender.com URL and the site is live.

Also create a free Postgres database on Render (New > PostgreSQL), copy its
Internal Database URL, and add it as a DATABASE_URL environment variable on the
web service. Without this, the free tier wipes your user accounts every time
the service restarts.

## 3. Update Google Cloud

In your Google Cloud project (the one with YouTube Data API v3):

1. APIs & Services > Credentials > your OAuth client:
   - Add authorized redirect URI:  https://YOUR-RENDER-URL/oauth/callback
   - Later, when the domain is connected, also add
     https://yourdomain.com/oauth/callback
2. While your app is in Testing mode, add each beta user's Gmail under
   OAuth consent screen > Test users. Up to 100 people, and their sign-in
   expires every 7 days, so beta users re-connect weekly. That is a Google
   rule, not a Poster bug.

## 4. Connect your domain

1. In Render: your service > Settings > Custom Domains > add yourdomain.com.
   Render shows you a CNAME/A record.
2. In your domain registrar's DNS settings, add that record.
3. Wait for it to verify (minutes to an hour). HTTPS is automatic.
4. Change APP_URL on Render to https://yourdomain.com and add the new
   redirect URI in Google Cloud (step 3).

## 5. Google verification (start this week, takes time)

To let anyone sign in, not just test users, Google must verify the app
because youtube.upload is a sensitive scope:

1. You need a privacy policy page and a terms page on your domain. Simple
   text pages are fine.
2. OAuth consent screen > Publish app, then follow the verification flow:
   app name, logo, links to those pages, a short explanation of why you
   need the upload scope ("users post their own videos to their own
   channel"). They may ask for a short screen recording of the flow.
3. Review usually takes several days to a few weeks. Until it clears,
   run the beta with manually added test users.

## 6. Stripe, when you're ready to charge

1. Make a Stripe account, create a Product "Poster" with a $10/month price,
   copy the price id (starts with price_).
2. Add env vars on Render: STRIPE_SECRET_KEY, STRIPE_PRICE_ID.
3. In Stripe: Developers > Webhooks > add endpoint
   https://yourdomain.com/api/stripe-webhook listening to
   checkout.session.completed, customer.subscription.deleted, and
   invoice.payment_failed. Copy the signing secret into STRIPE_WEBHOOK_SECRET.
4. Until those keys are set, the Subscribe button says billing isn't on yet,
   and trials just run out. So you can launch the beta before Stripe exists.

## What changed from your personal version

- No MCP bridge, no Claude Desktop. Metadata comes from the Claude API on
  the server, using your ANTHROPIC_API_KEY. Costs you a cent or three per
  video.
- No folder scanning. Users pick a video file in the browser; the browser
  samples the frames itself and uploads straight to YouTube. Videos never
  touch the server, which keeps the free tier viable.
- Users sign in with YouTube. Their refresh token is stored per account, so
  they connect once (7-day limit only while unverified).
- 7-day free trial per account, then the paywall, once Stripe is on.
- Dropped for v1: sound library, ideas tab, scheduling, commands. Add them
  back once people are paying.
