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

The three constraints above eliminate most of the field before you compare
anything. Checked September 2026 — **verify current terms yourself, because
these change often and two of them changed materially in the last year.**

| Host | Always on | Real disk | Free permanently | |
|---|---|---|---|---|
| **Oracle Cloud Always Free** | yes | yes, 200 GB | yes | **the default** |
| Google Cloud `e2-micro` | yes | yes, 30 GB | yes, no expiry | fallback |
| Fly.io | yes | $0.15/GB/mo | **no longer** | ~$2-4/mo |
| Koyeb free | no | no | yes | fails both |
| Render free | no | no | yes | schedule dies |
| Railway | yes | yes | no, $5 credit | runs out |
| Cloud Run | no | no | yes | needs external cron |
| AWS / Azure | 12 months, then billed | | no | no permanent free VM |

### The default: Oracle Cloud Always Free

The only host that meets all three constraints permanently, and it has a
**Mumbai region** — closest to the exchange feed.

- ✅ genuinely always on, real block storage, free indefinitely rather than as
  a trial
- ✅ a bare VM, so the in-process scheduler survives exactly as written and
  nothing has to be restructured
- ⚠️ signup wants a card for verification, and Mumbai capacity is sometimes
  unavailable — retry, or fall back to Google below
- ⚠️ you manage the OS, Python and a `systemd` unit yourself

Oracle **halved** the Always Free ARM allowance in June 2026, from 4 OCPU /
24 GB to 2 OCPU / 12 GB, enforced from 18 August 2026, terminating instances
over the limit. Irrelevant here: this app wants ~512 MB, so 12 GB is roughly
twenty times what it needs. The 200 GB of block storage was not touched.

Full walkthrough in **Deploying to Oracle** below.

### Fallback: Google Cloud `e2-micro`

The only permanent free VM among the big three, with no 12-month expiry. Take
it if Oracle has no capacity in a region you want.

- ✅ always on, persistent disk, free with no expiry
- ⚠️ US regions only (`us-west1`, `us-central1`, `us-east1`), so every NSE
  request crosses an ocean
- ⚠️ 1 GB/month egress, and `e2-micro` is 1 GB shared RAM — workable but tight
  next to Oracle's headroom

The Oracle walkthrough below applies unchanged apart from the provisioning
step; it is the same Ubuntu VM and the same `systemd` unit.

### Fly.io — no longer free

Fly withdrew its free tier for accounts created after **October 2024**. New
signups get a short trial, and volumes bill at $0.15/GB/month whether or not
the machine is running. A small always-on machine here is a few dollars a
month, not zero.

`fly.toml` and `Dockerfile` are still in this repo and still correct, so this
remains a good paid option, or a free one on a legacy account:

```bash
fly launch --no-deploy
fly volumes create data --size 1 --region bom
fly secrets set ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
fly deploy
```

### Not suitable

- **Koyeb free** — looks ideal until the details: free instances force
  scale-to-zero after an hour idle and **cannot** be disabled, and free
  instances cannot attach volumes at all. It fails both hard constraints.
- **Render free tier** — web services sleep after 15 minutes idle; cron jobs
  are a paid feature. The schedule will not fire.
- **Railway** — always-on, but on trial credit rather than a free tier. It
  stops when the credit does.
- **Google Cloud Run** — scales to zero with an ephemeral filesystem. Workable
  only by dropping the in-process scheduler for Cloud Scheduler POSTing to
  `/start`, and moving the database off local disk. That trades away the part
  of this project that understands market phase.
- **AWS / Azure** — free instances are 12-month promotions that silently
  convert to paid. Neither has a permanent free VM.
- **Vercel / Netlify functions** — serverless, no long-lived threads, no disk.
  Right for the frontend, wrong for this backend.
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

That writes **both pages** into `web/` with the backend URL baked into each:

| File | Served at | Contents |
|---|---|---|
| `index.html` | `/` | the dashboard and the agent run |
| `ipo-desk.html` | `/ipo-desk` | the IPO desk |

The dashboard links to `/ipo-desk`, which is the same path Flask serves locally.
That works on Vercel because `web/vercel.json` sets `cleanUrls`, which maps
`/ipo-desk` to `ipo-desk.html`. **Re-run the build whenever either page
changes** — a page left stale in `web/` is what actually gets served.

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

## Deploying to Oracle

End to end on a fresh Always Free VM. Roughly twenty minutes, most of it
waiting on Oracle's console.

### 1. The instance

Create a VM in the Oracle console:

- **Shape** `VM.Standard.A1.Flex` (ARM), 1 OCPU / 6 GB — inside the 2 OCPU /
  12 GB Always Free limit with room to spare. `VM.Standard.E2.1.Micro` (AMD)
  also works and is a separate allowance if ARM capacity is out.
- **Image** Canonical Ubuntu 24.04
- **Region** Mumbai (`ap-mumbai-1`) for latency to NSE
- Save the SSH keypair it offers — that is the only copy

Confirm the shape says **Always Free eligible** before you create it. A shape
outside the allowance bills silently.

### 2. Open the port

Two layers, and missing either one produces the same symptom: a server that
looks healthy over SSH and is unreachable from the internet.

In the console, add an ingress rule to the subnet's security list — source
`0.0.0.0/0`, TCP, destination port `8080`. Then on the box itself, because
Oracle's Ubuntu images ship with iptables already populated:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save
```

### 3. Install

```bash
ssh -i your-key.pem ubuntu@<public-ip>
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/ItsDee19/one-click.git && cd one-click
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env && nano .env
```

Set `ALLOWED_ORIGINS` to your exact Vercel origin, and add the Telegram token
and chat id. Leave `ANTHROPIC_API_KEY` unset to run deterministic — see the
constraints section on what that costs you, which is less than it sounds.

### 5. Run it as a service

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
Environment=PORT=8080
Environment=NO_BROWSER=1
ExecStart=/home/ubuntu/one-click/.venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dalal-desk
sudo journalctl -u dalal-desk -f
```

`Restart=always` matters: a yfinance hiccup that crashes the process would
otherwise silently end your schedule for the day. `NO_BROWSER=1` stops the app
trying to open a browser on a machine that has none.

### 6. Point the frontend at it

Back on your own machine, rebuild with the VM's address and redeploy:

```bash
python build_web.py --api http://<public-ip>:8080
cd web && vercel --prod
```

Then confirm the two halves agree:

```bash
curl http://<public-ip>:8080/health
```

`engine` tells you whether a key was picked up, and `scheduler.next_run_label`
tells you the schedule survived the boot.

### On HTTPS

A Vercel page is served over HTTPS, and a browser will refuse to call a plain
`http://` backend from it — mixed content is blocked outright. So either put a
certificate on the VM, or accept that the deployed frontend cannot reach it.

The cheapest fix is Caddy, which obtains and renews a certificate on its own.
It needs a domain name pointing at the VM; a free subdomain works:

```bash
sudo apt install -y caddy
echo 'your-domain.com { reverse_proxy 127.0.0.1:8080 }' | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Then open port 443 the same two ways as step 2, rebuild the frontend against
`https://your-domain.com`, and set `ALLOWED_ORIGINS` to your Vercel origin.

Until that is done, the deployed dashboard will load but stay empty. The
backend is reachable directly in a browser tab, and `curl` works regardless —
it is the browser's mixed-content rule, not the server.

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

Finally click **IPO desk** in the header, or visit `/ipo-desk` directly. A 404
there means the deployment is serving a `web/` built before the page existed;
re-run `build_web.py` and redeploy. The IPO desk fetches offer documents on
first load and can take a couple of minutes before the cards appear — that is
the DRHP parse, not a hang, and results are cached for 30 days afterwards.

### Rehearsing the split locally

The whole split can be exercised before paying for anything — this catches CORS
and baked-URL mistakes, which are most of what goes wrong:

```bash
python build_web.py --api http://127.0.0.1:5000
ALLOWED_ORIGINS=http://127.0.0.1:8000 python app.py
python -m http.server 8000 --directory web
```

Open `http://127.0.0.1:8000`. If the header fills in with the engine and market
regime, the cross-origin path works. Note that `/ipo-desk` 404s under
`http.server`, which does not implement `cleanUrls` — use `/ipo-desk.html`
locally; only Vercel serves the clean path.

---

## Secrets and the container image

`Dockerfile` ends with `COPY . .`, so `.dockerignore` is what keeps your `.env`
out of the image. Image layers are readable by anyone who can pull the image,
and a copied `.env` would also silently shadow the platform's own value for any
key the platform did not set. Pass secrets as real environment variables
instead — `fly secrets set`, or systemd's `EnvironmentFile` — and leave `.env`
for local development only.
