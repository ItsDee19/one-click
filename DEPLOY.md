# Deploying Dalal Desk

Split deployment: a **static frontend** on Vercel, and a **backend that runs
continuously** somewhere else.

Read the constraints section first. Two of them will decide which option makes
sense for you, and one of them costs money you are not currently spending.

---

## The constraints, before you pick a host

### 1. The `claude` CLI will not work on a cloud box

This is the big one. Right now the LLM panel runs through your Claude
subscription via the `claude` CLI, which costs you nothing extra. That
authentication is an interactive browser login stored in `~/.claude` on your
machine. It does not transfer to a server, and there is no supported way to log
in headlessly.

On any cloud host you have three options:

| Option | What happens | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | full LLM panel, unchanged | per-token API billing, **not** covered by your subscription |
| `OPENAI_API_KEY` | full LLM panel via OpenAI | per-token billing |
| nothing | falls back to `scoring.py` | free, still fully functional |

The deterministic fallback is not a degraded mode — it produces the same
verdicts, thresholds, sizing and risk gates. What you lose is the argued
rationale. If you deploy without a key, the footer will honestly say
`deterministic` and everything else works.

**Rough API cost if you do use a key:** ~12 stocks per run × ~12 runs/day ≈ 144
calls/day. On Haiku that is small; on Sonnet with the full evidence bundle it
is materially more. Start with `CLAUDE_CLI_MODEL=haiku` equivalent
(`ANTHROPIC_MODEL=claude-haiku-4-5-20251001`) and watch the bill for a week
before moving to Sonnet.

### 2. SQLite needs a real disk

`signals.db` holds the entire track record — every settled outcome, the
confidence calibration, the paper account. That is the whole point of the
memory system.

Most free tiers give you an **ephemeral filesystem**: the database is wiped on
every deploy and on every restart. A host with a persistent volume is not
optional here unless you are willing to lose your history repeatedly.

### 3. Free tiers that sleep will break the schedule

The scheduler is a thread inside the process. A host that spins the container
down after 15 minutes of no HTTP traffic will kill it, and your 09:00 run will
not happen. Anything marketed as "free web service, sleeps when idle" is the
wrong shape for this.

---

## Backend hosting

Ranked for this specific workload. **Verify current free-tier terms yourself —
they change often, and some of these have tightened since.**

### Recommended: Oracle Cloud Always Free

A genuine always-on VM, persistent disk, no sleep, free indefinitely rather
than as a trial. ARM instances offer very generous specs.

- ✅ always on, real disk, cron if you want it
- ✅ enough resources to run Sonnet-class workloads comfortably
- ⚠️ signup requires a card for verification and capacity is sometimes
  unavailable in popular regions
- ⚠️ it is a bare VM: you manage the OS, Python, and a `systemd` unit

```bash
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/ItsDee19/one-click.git && cd one-click
pip3 install -r requirements.txt
cp .env.example .env && nano .env        # add your keys
```

Then a service so it survives reboots — see `systemd` below.

### Fly.io

Container platform with persistent volumes and no forced sleep on small apps.
Closest thing to "just push it" that still meets the constraints.

- ✅ persistent volume for SQLite, always-on
- ✅ simple deploy, region can be set to Mumbai (`bom`) for latency
- ⚠️ free allowance has narrowed; a small always-on machine may incur a few
  dollars a month
- A `fly.toml` and `Dockerfile` are included in this repo

```bash
fly launch --no-deploy
fly volumes create data --size 1 --region bom
fly secrets set ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
fly deploy
```

### Google Cloud Run + Cloud Scheduler

Scales to zero, so you pay nothing while idle — but that is exactly the sleep
problem. Workable only if you **drop the in-process scheduler** and let Cloud
Scheduler POST to `/start` on a cron, which means restructuring how runs are
triggered.

- ✅ genuinely free at this volume
- ❌ ephemeral filesystem: needs Cloud SQL or GCS for the database
- ❌ the elegant part of this project (a scheduler that understands market
  phase) gets replaced by external cron

### Not suitable

- **Render free tier** — web services sleep after 15 minutes idle; cron jobs
  are a paid feature. The schedule will not fire.
- **Vercel / Netlify functions** — serverless, no long-lived threads, no disk.
  Fine for the frontend, wrong for this backend.
- **PythonAnywhere free** — outbound network is allowlisted, and Yahoo
  Finance is not on the allowlist.
- **Heroku** — no meaningful free tier any more.

### Honestly: consider not deploying the backend at all

Your PC already does this well, for free, with your subscription auth working
and a real disk. If the only reason to move is "so it runs while my laptop is
shut", Windows Task Scheduler solves that more cheaply than any cloud host:

```
Task Scheduler → Create Task
  Trigger : Daily, 08:55, repeat every 5 min for 8 hours
  Action  : python.exe  D:\one-lick\app.py
  Settings: "Run whether user is logged on or not"
```

The app's own scheduler then handles 09:00, 09:45 and the session sweeps, and
stands down on holidays by itself.

---

## Frontend on Vercel

```bash
python build_web.py --api https://your-backend-url
```

That writes `web/index.html` with the backend URL baked in. Then:

```bash
cd web && vercel --prod
```

Or point Vercel at this repo with **Root Directory = `web`**, framework preset
**Other**, and no build command.

### The step people forget

The backend must allow your Vercel origin, or every request is blocked by CORS
and the dashboard shows "could not reach the backend":

```bash
ALLOWED_ORIGINS=https://your-project.vercel.app
```

Comma-separate for previews. `*` is accepted but do not use it — this API
exposes your positions, track record and paper account to whoever asks.

### Overriding the backend without rebuilding

The baked URL is only a default:

```
https://your-project.vercel.app/?api=http://127.0.0.1:5000
```

The page remembers that in `localStorage`, so the same deployment can point at
a local backend while you develop.

---

## systemd unit (Oracle / any Linux VM)

```ini
# /etc/systemd/system/dalal-desk.service
[Unit]
Description=Dalal Desk
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/one-click
EnvironmentFile=/home/ubuntu/one-click/.env
Environment=HOST=0.0.0.0
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now dalal-desk
sudo journalctl -u dalal-desk -f
```

`Restart=always` matters: a yfinance hiccup that crashes the process would
otherwise silently end your schedule for the day.

---

## Environment for a deployed backend

```bash
HOST=0.0.0.0                                   # required in a container
PORT=8080                                      # most platforms inject this
ALLOWED_ORIGINS=https://your-project.vercel.app
TZ=Asia/Kolkata                                # see below

SCHEDULE_ENABLED=1
SCHEDULE_TIMES=09:00,09:45
SCHEDULE_INTERVAL_MINUTES=30
SCHEDULE_FOLLOW_SESSION=1

ANTHROPIC_API_KEY=...                          # or omit for deterministic
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Timezone:** every scheduling decision is computed in IST explicitly
(`market.IST`), so the container's own clock zone does not affect *when* runs
fire. Setting `TZ=Asia/Kolkata` only makes the platform's log timestamps match
the app's, which is worth it when you are debugging why something did or did
not run.

---

## Verifying a deployment

```bash
curl https://your-backend/health
```

Check four things in the response:

| Field | What it tells you |
|---|---|
| `trading_day.trading` | whether the exchange says today is a session |
| `market.phase` | pre-open / regular / closed, computed in IST |
| `scheduler.next_run_label` | the schedule survived the deploy |
| `engine` | `deterministic` means no API key was picked up |

Then open the Vercel URL. If the KPIs stay blank and you see a CORS error in
the browser console, `ALLOWED_ORIGINS` does not list that exact origin —
including the scheme, and with no trailing slash.
