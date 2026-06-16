# Car Wash Scout Agent

Runs every morning, scrapes BizBuySell and LoopNet for US car wash listings,
and automatically adds any new ones to your Shullman Car Wash Scout site.

## What it does

1. Calls your site's API to get the current list of listings
2. Scrapes BizBuySell (`/car-washes-for-sale/`) — up to 20 pages
3. Scrapes LoopNet (`/biz/car-washes-for-sale/`) — best-effort (heavily protected)
4. For each listing NOT already on your site, POSTs it via `/api/manual-records`

New listings appear on your site immediately after each run.

## Deploy to Render (step by step)

### Step 1 — Push this folder to GitHub

Create a new GitHub repo and push the contents of this `carwash-scout-agent` folder to it.

```bash
cd carwash-scout-agent
git init
git add .
git commit -m "Car wash scout agent"
git remote add origin https://github.com/YOUR_USERNAME/carwash-scout-agent.git
git push -u origin main
```

### Step 2 — Create the service on Render

1. Go to [render.com](https://render.com) → **New** → **Blueprint**
2. Connect your GitHub account and select the `carwash-scout-agent` repo
3. Render will detect `render.yaml` automatically — click **Apply**

### Step 3 — Set the environment variable

In the Render dashboard for the new service:

1. Click **Environment**
2. Add a new variable:
   - **Key:** `SITE_URL`
   - **Value:** `https://shullman-carwash-scout.onrender.com`

### Step 4 — Done!

The agent runs every day at 8 AM Eastern. You can also trigger a manual run from the
Render dashboard by clicking **Manual Run** on the cron job.

## Check the logs

In Render → your `carwash-scout-agent` service → **Logs**. You'll see:
- How many existing listings were found
- Each new listing discovered (prefixed with `+`)
- Confirmation that it was added to your site

## A note on LoopNet

LoopNet uses aggressive bot-protection (Cloudflare). The agent handles this gracefully —
if it gets blocked it logs a warning and continues. BizBuySell is the more reliable source.
