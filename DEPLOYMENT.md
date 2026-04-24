# Credentials Bot — Full Deployment Guide

## What's in this package

```
credentials-bot/
  main.py              ← Bot + dashboard (single file, runs both)
  requirements.txt
  railway.toml         ← Deploy config for Railway
  render.yaml          ← Deploy config for Render
  .env.example         ← Environment variables template
  templates/
    index.html         ← Landing page
    dashboard.html     ← Server selector
    server.html        ← Per-server control panel
  static/
    style.css          ← Landing page styles
    dash.css           ← Dashboard styles
    logo.svg           ← Bot logo
```

---

## Step 1 — Create Discord Application

1. Go to https://discord.com/developers/applications
2. New Application → name it anything
3. **Bot** tab → Add Bot → copy the **Token**
4. Enable ALL THREE Privileged Intents:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
5. **OAuth2** tab:
   - Copy **Client ID** and **Client Secret**
   - Under Redirects → Add: `https://your-render-url.onrender.com/callback`
6. Invite the bot:
   - OAuth2 → URL Generator
   - Scopes: `bot`
   - Permissions: `Administrator`
   - Open the URL and add to your server

---

## Step 2 — Deploy on Render (Bot + Dashboard together)

Everything runs from `main.py` — Flask dashboard + Discord bot in one process.

1. Push this folder to GitHub:
```bash
git init
git add .
git commit -m "credentials bot"
git remote add origin https://github.com/YOU/credentials-bot.git
git push -u origin main
```

2. Go to https://render.com → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
5. Environment Variables → Add all of these:
   - `DISCORD_TOKEN` = your bot token
   - `DISCORD_CLIENT_ID` = your client ID
   - `DISCORD_CLIENT_SECRET` = your client secret
   - `DISCORD_REDIRECT_URI` = `https://your-render-url.onrender.com/callback`
   - `SECRET_KEY` = any random string (used for sessions)
6. Deploy. Copy your `.onrender.com` URL.
7. Go back to Discord Dev Portal → OAuth2 → Redirects → update the URL to match.

**Note:** Render free tier spins down after inactivity. For 24/7 uptime use the $7/month plan.

---

## Step 3 — First-Time Bot Setup (in your Discord server)

Once the bot is online:

```
/prefix !                    → Change prefix
!logging #mod-logs           → Set log channel
!bypass add @Admin           → Admins bypass all restrictions
!welcome channel #welcome    → Set welcome channel
!welcome message Welcome {user} to {server}! You are member #{count}.
!verification setup          → Auto-creates Unverified role + verify channel
```

Or do all of it through the dashboard at your Render URL.

---

## Dashboard Features

Login with Discord → select a server → full control panel:

- **Overview** — server stats at a glance
- **General** — prefix, log channel, autorole
- **Moderation** — bypass roles, per-command role restrictions
- **Verification** — enable/disable, unverified role, verified role, channel, message, min account age, kick bots, DM on join
- **Welcome & Leave** — channels and messages with variable support
- **Tickets** — category, support role
- **Auto-Role** — on join role assignment

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token |
| `DISCORD_CLIENT_ID` | Yes | OAuth2 client ID |
| `DISCORD_CLIENT_SECRET` | Yes | OAuth2 client secret |
| `DISCORD_REDIRECT_URI` | Yes | `https://yoursite.onrender.com/callback` |
| `SECRET_KEY` | Yes | Random string for Flask sessions |
| `PORT` | No | Port (auto-set by Render) |

---

## Verification System Commands

```
!verification setup              → Auto-creates Unverified role + #verify channel + sends panel
!verification enable             → Enable verification
!verification disable            → Disable verification
!verification panel [#channel]   → Resend the verify button panel
!verification setrole unverified @role
!verification setrole verified @role
!verification minage <days>      → Require account to be X days old
```

Or configure everything in the dashboard under Verification.

