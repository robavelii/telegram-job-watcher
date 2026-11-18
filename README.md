# telegram-job-watcher

[![CI](https://github.com/robavelii/telegram-job-watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/robavelii/telegram-job-watcher/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Reads the Telegram job channels you follow, keeps the jobs that fit you and that you can
actually take from where you live, and sends them to you as alerts.**

It watches every channel in one Telegram folder. For each new post it uses an LLM (Gemini or
Claude) to decide whether the post is a job, translate it into English, check whether
someone in your country can apply, and score it 0–100 against your resume. Only the good
ones reach your phone, through a bot of your own.

## Why

Many good remote jobs are posted only in Telegram channels, often in Ukrainian or Russian,
mixed with ads, courses and reposts. Following a few dozen channels means reading hundreds of
posts a day, and many of the ones that look right turn out to be "remote, Ukraine only" or to
need an EU work permit. This is a real problem if you live somewhere else.

This tool was built to do that reading for one job seeker outside the channels' home
countries: translate everything, throw away what isn't a job or isn't open to them, and ping
only for roles that match their resume. A daily summary links every post it read, so you can
check what the model decided and why.

## Features

- **Any language in, English out.** Each post comes back as a title, company, stack,
  salary, a short summary, why it fits, what's missing and how to apply.
- **Location-aware.** Each job is marked open, closed or unclear for your
  `CANDIDATE_LOCATION`. Posts limited to other countries can't score above 30, and the
  code enforces that cap as well as the prompt.
- **Scored against your resume and preferences.** You choose the alert threshold; near
  misses go into the daily summary instead.
- **Reads with your account, writes with a bot.** Your account never posts, joins
  anything or marks posts as read.
- **Cheap.** Short posts and ads are filtered out before any API call. Near-duplicate
  reposts across channels are scored once. Gemini's free tier is enough for most people.
- **Hands-off.** The folder is re-read every 15 minutes, so adding a channel needs no
  restart. After downtime it catches up on missed posts. It can hold alerts during quiet
  hours.
- **No inbound ports.** It only makes outbound connections, so it runs on any machine
  with Docker.

## How it works

```
Telegram (your user account, MTProto, read-only)
  │ new post in a channel in your folder
  ▼
listener ── pre-filter (length, ad markers) ── dedupe (SQLite, exact + near-duplicate)
  │
  ▼
scorer: one Gemini or Claude call → strict JSON
        (is it a job? translation, open to CANDIDATE_LOCATION?, fit 0-100)
  │ job, fit ≥ FIT_THRESHOLD, not closed to your location
  ▼
notifier: Telegram bot message to you (held during quiet hours)
```

An alert looks like this:

```
✅ 84/100 · Senior Backend Engineer (Python)
🏢 Orbital Labs
💼 Senior · Full-time · $4,000–5,500
🧰 Python, FastAPI, PostgreSQL, AWS
🌍 Open to you: yes (worldwide, async team)

Builds the billing API for a B2B SaaS product…

Why it fits
• 5 years of Python and FastAPI
• …

Apply: @orbital_hr
📎 Remote IT Jobs
```

## What you need

- A Telegram account, and the job channels you want watched in one
  [chat folder](https://telegram.org/blog/folders) (for example `remote-jobs`).
- A Telegram bot of your own for the alerts (free, about a minute with @BotFather).
- A model API key: [Gemini](https://aistudio.google.com/apikey) (free tier) or
  [Claude](https://platform.claude.com).
- Docker with Compose on the machine that runs it: a laptop that stays on, a home server
  or a small VPS. The container is limited to 256 MB of memory.
- Python 3.12+ on your own computer, once, to log in to Telegram.

## Setup

### 1. Get the code

```bash
git clone https://github.com/robavelii/telegram-job-watcher.git
```

```bash
cd telegram-job-watcher && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

### 2. Telegram API credentials

1. Log in at https://my.telegram.org with your phone number in international format. The
   code arrives as a message from "Telegram" inside the Telegram app, not by SMS.
2. Open **API development tools** and fill in the form. For example: App title
   `job-watcher`, Short name `jobwatcher`, Platform `Desktop`. URL and description can
   stay empty.
3. Note `App api_id` (a number) and `App api_hash` (32 hex characters). Treat the hash
   like a password.

If the form says "ERROR", try again later or from another network. my.telegram.org often
refuses VPN and datacentre IP addresses.

### 3. Session string

```bash
.venv/bin/python gen_session.py --out session.txt
```

It asks for the `api_id` and `api_hash`, then shows a QR code. On your phone, go to
Telegram → Settings → Devices → **Link Desktop Device** and scan it. It also asks for your
two-step verification password if you have one. When it's done, it prints your user id
(this is your `NOTIFY_CHAT_ID`) and writes the session string to `session.txt`. If QR
login gives you trouble, use `--phone` instead.

> The session string gives full access to your Telegram account. Never share or commit it.

### 4. Alert bot

1. In Telegram, message **@BotFather** (it has a blue check mark) and send `/newbot`.
2. Give the bot a name and a username ending in `bot`, for example `my_job_watcher_bot`.
3. Copy the token BotFather sends you (`123456789:AA…`). This is your `BOT_TOKEN`.
4. Open the new bot from BotFather's message and press **Start**. A bot can't message
   you until you do.

### 5. Configure

```bash
cp .env.example .env && chmod 600 .env
```

```bash
cp config/resume.example.md config/resume.md && cp config/preferences.example.yaml config/preferences.yaml && chmod 600 config/resume.md config/preferences.yaml
```

```bash
sed -i "s/^WATCHER_UID=.*/WATCHER_UID=$(id -u)/" .env
```

The last command lets the container read your mode-600 config files. On macOS, edit
`WATCHER_UID` in `.env` by hand instead.

Fill in `.env`. These are required:

| Variable | Value |
|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | From step 2 |
| `TELEGRAM_SESSION_STRING` | The contents of `session.txt` from step 3 (then delete the file) |
| `TELEGRAM_FOLDER_NAME` | Name of your Telegram folder of job channels (default `remote-jobs`) |
| `BOT_TOKEN` | From step 4 |
| `NOTIFY_CHAT_ID` | Your user id, printed in step 3 |
| `CANDIDATE_LOCATION` | The country you work from, e.g. `Kenya` or `United Kingdom` |
| `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` | Your model key |
| `TZ_NAME` | Your time zone, e.g. `Europe/Berlin` (default `UTC`) |

Then fill in `config/resume.md` (your resume as plain text or Markdown) and
`config/preferences.yaml`: target roles, stack, minimum rate, and anything that should
never alert. The model reads both files on every post, so describe what you want the way
you would to a recruiter. Leave out your contact details; the model doesn't need them.

### 6. Run

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

Within a minute the log shows a line like `folder 'remote-jobs': N chats`, and from then
on new posts are scored as they arrive. `docker compose ps` reports `healthy` about three
minutes after the start.

To see alerts in the log before any reach your phone, set `DRY_RUN=1` for the first run.
To score some recent posts straight away, set `BACKFILL_PER_CHANNEL=20` before the first
start, or send `/scan` to your bot.

### Running it on a server

The steps are the same on any Linux machine with Docker. Clone the repo there and create
`config/resume.md` and `config/preferences.yaml` on the server (step 5). The secrets don't
have to be typed into a remote shell. From your computer, run:

```bash
scripts/push-secrets.sh user@your-server ~/.ssh/id_ed25519 telegram-job-watcher
```

It asks for each value without echoing it and writes it into the server's `.env` over SSH.
The last argument is the clone's path on the server, relative to your home directory. It
reads `./session.txt` if it exists and deletes the file afterwards.

To update:

```bash
git pull --ff-only && docker compose up -d --build
```

`.env` and `config/` are gitignored, so `git pull` never touches them.

## Using it

### Bot commands

Send these to your bot. It answers only `NOTIFY_CHAT_ID` and ignores everyone else.

| Command | What it does |
|---|---|
| `/summary` | Every post read in the last 24 hours, grouped: alerts, near misses, low fit, not open to your location, not jobs, errors, skipped and duplicates. Each one links to the original message, so you can check what the model got wrong. `/summary 48` gives another window. A long summary arrives as several messages |
| `/top` | The best job posts of the last 24 hours that are open to you, whatever their score. `/top 10` |
| `/status` | Whether it's connected, chats watched, last post read, model, summary time |
| `/scan` | Re-reads the last 24 hours of every chat (up to 72: `/scan 6`). Posts already seen are skipped and alerts arrive as usual. It takes minutes, and on the Gemini free tier it uses quota |
| `/help` | The list above |

If `DAILY_DIGEST_AT` is set, the same summary also arrives every day at that time.

### Try it end to end

1. Create a private test channel and add it to your folder.
2. Wait for the log line `folder '<name>': N chats (+1 -0)`. This takes up to 15
   minutes, or run `docker compose restart`.
3. Paste one of `tests/fixtures/posts/*.txt` into the channel. The alert should arrive
   within seconds; posts you write yourself count too.

### Settings

All settings live in `.env`; [`.env.example`](.env.example) documents every one. These are
the ones you're most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `FIT_THRESHOLD` | `70` | Minimum fit score for an alert |
| `NEAR_MISS_MIN` | `50` | Scores from here up to the threshold are listed as near misses in summaries |
| `QUIET_HOURS` | off | e.g. `23:00-07:00` in `TZ_NAME`; alerts are held and sent when it ends |
| `DAILY_DIGEST_AT` | off | e.g. `20:00`; sends the `/summary` of the last 24 hours every day |
| `MIN_POST_CHARS` | `200` | Shorter posts never reach the API |
| `BACKFILL_PER_CHANNEL` | `0` | First start only: score the last N posts of each channel |
| `LLM_PROVIDER` | the key that is set | `gemini` or `anthropic` |
| `LLM_MODEL` | per provider | `gemini-3.5-flash-lite` or `claude-haiku-5-5` |
| `CLAUDE_EFFORT` | `medium` | Claude only: `low` is cheaper, `high` thinks harder |

### Cost

- **Gemini.** The free tier costs nothing but has per-minute and per-day request limits
  (see https://aistudio.google.com/rate-limit). The client retries rate-limited requests
  for about two minutes. Posts that still fail are recorded and listed in the summary.
- **Claude.** Haiku 5.5 costs $0.10 per million input tokens and $0.50 per million output
  tokens, with the resume part of the prompt cached. That works out to roughly a tenth of
  a cent per scored post (an estimate).
- **Either way,** short posts, ads and duplicates never reach the API.

## Privacy

Your resume, your preferences and every scored post are sent to the model provider you
choose.

- **Gemini free tier:** Google's terms for unpaid services let it use prompts and
  responses to improve its products, and human reviewers may read them. Keep contact
  details out of `config/resume.md`, and enable billing on the Google Cloud project if this
  matters to you. Paid use is not used for training.
- **Claude API:** inputs and outputs are not used for training by default.

Apart from Telegram and the model provider, nothing leaves the machine. Post texts and
results stay in a local SQLite file in the `watcher-data` Docker volume.

## Security

- `TELEGRAM_SESSION_STRING` gives full access to your Telegram account. Keep `.env` at
  mode 600 and never commit it. If it leaks, go to Telegram → Settings → Devices,
  terminate the `job-watcher` session, and run `gen_session.py` again.
- Post text is treated as untrusted input. The model has no tools, posts are wrapped in
  delimiters, the prompt tells it to ignore instructions inside them, and the location cap
  is enforced in code.
- The bot token never appears in logs, because HTTP request logging is turned off.
- The container runs as a non-root user with a read-only filesystem, no Linux capabilities
  and no open ports.

## Development

```bash
.venv/bin/python -m pytest
```

`tests/test_live_scorer.py` sends the sample posts to the real model. It's skipped unless
you set `RUN_LIVE=1` and `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`.

| Path | What |
|---|---|
| `watcher/listener.py` | Telethon client, folder resolution, new-post handler, missed-post sync |
| `watcher/pipeline.py` | Pre-filter → dedupe → score → decide → notify, for one post |
| `watcher/scorer.py` | Prompt, JSON schema and result checks |
| `watcher/backends.py` | Gemini and Claude calls (`LLM_PROVIDER`) |
| `watcher/notifier.py` | Bot API sender, quiet-hours outbox, alert and summary formatting |
| `watcher/commands.py` | Bot commands |
| `watcher/store.py` | SQLite: seen posts, results, outbox |
| `watcher/main.py` | Entry point, reconnect loop, scheduler (heartbeat, outbox, digest) |
| `gen_session.py` | Creates the Telegram session string. Run it on your own computer |
| `scripts/push-secrets.sh` | Writes secrets into a server's `.env` over SSH without echoing them |

Issues and pull requests are welcome.

## License

[MIT](LICENSE)
