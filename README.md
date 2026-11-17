# telegram-job-watcher

Watches every channel in one Telegram folder (`remote-jobs`), decides whether each new
post is a job, translates it, scores it against your resume with Gemini or Claude, and pings you
through a bot when it fits.

```
Telegram (your user account, MTProto, read-only)
  │ new post in a folder channel
  ▼
listener ── pre-filter (length, ad markers) ── dedupe (SQLite, exact + near-duplicate)
  │
  ▼
scorer: one Gemini or Claude call → strict JSON (is job? translation, eligibility, fit 0-100)
  │ job, fit ≥ FIT_THRESHOLD, eligible from Ethiopia ≠ "no"
  ▼
notifier: Bot API sendMessage to your own chat (held during quiet hours)
```

- **Reads with your account, writes with a bot.** The user session never sends,
  marks as read, or joins anything. The bot answers commands from your chat only.
- **No inbound ports.** Only outbound connections to Telegram and the model API,
  so there is nothing to add to Caddy or the firewall.
- Folder changes are picked up every 15 minutes without a restart.
- Edits never re-alert. Albums are scored once, from the captioned item.
- After a restart it fetches up to 50 posts per channel that it missed while down.
- Post text is untrusted: the model has no tools, the post is wrapped in delimiters,
  and the location cap is enforced in code as well as in the prompt.

## Do I need a bot?

Yes. The design never sends from your account, and a message you send yourself
(Saved Messages) does not notify you anyway. A bot's message is a normal push
notification. Make a new one with @BotFather (`/newbot`, about a minute) and press
**Start** on it once. Reusing another bot's token also works, since the watcher only
calls `sendMessage` and never reads updates, but a separate bot keeps these alerts in
their own chat.

## Files

| Path | What |
|---|---|
| `watcher/listener.py` | Telethon client, folder resolution, new-post handler, missed-post sync |
| `watcher/pipeline.py` | Pre-filter → dedupe → score → decide → notify for one post |
| `watcher/scorer.py` | Prompt, JSON schema and result checks |
| `watcher/backends.py` | Gemini and Claude calls (`LLM_PROVIDER`) |
| `watcher/notifier.py` | Bot API sender, quiet-hours outbox, alert and digest formatting |
| `watcher/store.py` | SQLite: seen posts, results, outbox |
| `watcher/main.py` | Entry point, reconnect loop, scheduler (heartbeat, outbox, digest) |
| `gen_session.py` | Makes the session string. Run it on your laptop |
| `config/*.example` | Templates for `resume.md` and `preferences.yaml` (both gitignored) |

## 1. On your laptop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

**Telegram `api_id` and `api_hash`** (once per account):

1. Open https://my.telegram.org in a browser and log in with your phone number in
   international format (`+251…`). The login code arrives as a message from "Telegram"
   inside the Telegram app, not by SMS.
2. Click **API development tools**.
3. Fill in the form: App title `job-watcher`, Short name `jobwatcher` (letters and
   digits, 5–32 characters), Platform `Desktop`. URL and description can stay empty.
4. Click **Create application**. The page shows `App api_id` (a number) and
   `App api_hash` (32 hex characters). Treat the hash like a password.

If the form says "ERROR", try again later or from another network; my.telegram.org is
known to refuse VPN and some datacentre IPs.

**Session string** (QR login):

```bash
.venv/bin/python gen_session.py --out session.txt
```

It asks for `api_id` and `api_hash`, then draws a QR code. On your phone: Telegram →
Settings → Devices → **Link Desktop Device**, and scan it. If you use two-step
verification, it then asks for that password. It prints your user id, which is your
`NOTIFY_CHAT_ID`, and writes the session string to `session.txt`. Use `--phone` instead
if QR login gives trouble.

**Notifier bot:**

1. In Telegram, open a chat with **@BotFather** (blue check mark) and send `/newbot`.
2. Give it a display name (e.g. `Job Watcher`) and a username ending in `bot`
   (e.g. `my_job_watcher_bot`).
3. BotFather replies with the token, `123456789:AA…`. That is `BOT_TOKEN`.
4. Open your new bot from the link in that message and press **Start**. A bot cannot
   message you until you do.

**Model key:** Gemini at https://aistudio.google.com/apikey (free tier; read
[Privacy](#privacy) first), or Claude at https://platform.claude.com.

**Send them to the server** without them appearing on screen or in a shell history:

```bash
scripts/push-secrets.sh ubuntu@your-server ~/.ssh/your_server_key
```

It reads the session string from `./session.txt` and deletes the file afterwards.

Run the tests:

```bash
.venv/bin/python -m pytest
```

`tests/test_live_scorer.py` sends the sample posts to the real model and is skipped
unless you set `RUN_LIVE=1` and `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`.

## 2. On the OCI server

The host already runs several compose projects behind a shared system Caddy
(`/etc/caddy/sites/*.caddy`, ports 80/443). This service publishes no ports and needs
no site file, so none of them are touched. Compose project name: `job-watcher`.

```bash
ssh -i ~/.ssh/your_server_key ubuntu@your-server
```

Read-only deploy key for the clone, same pattern as the other apps:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/job_watcher_deploy -C job-watcher-deploy
```

```bash
printf '\nHost github-job-watcher\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/job_watcher_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
```

```bash
cat ~/.ssh/job_watcher_deploy.pub
```

Add that key on your laptop (read-only by default):

```bash
gh repo deploy-key add job_watcher_deploy.pub -R robavelii/telegram-job-watcher --title my-server
```

Back on the server:

```bash
sudo install -d -o ubuntu -g ubuntu /opt/job-watcher
```

```bash
git clone git@github-job-watcher:robavelii/telegram-job-watcher.git /opt/job-watcher/app
```

```bash
cd /opt/job-watcher/app && cp .env.example .env && chmod 600 .env && nano .env
```

```bash
cp config/resume.example.md config/resume.md && cp config/preferences.example.yaml config/preferences.yaml && chmod 600 config/resume.md config/preferences.yaml
```

Set the container's user id to yours, so it can read those mode-600 files:

```bash
sed -i "s/^WATCHER_UID=.*/WATCHER_UID=$(id -u)/" .env
```

Fill in both files, then start it. Set `DRY_RUN=1` in `.env` for the first run if you
want to see alerts in the logs before any reach your phone.

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

`docker compose ps` shows `healthy` about three minutes after start. `.env`,
`config/resume.md` and `config/preferences.yaml` are gitignored, so `git pull` never
touches them. Do not run `git clean -x` in the clone.

**Update:**

```bash
cd /opt/job-watcher/app && git pull --ff-only && docker compose up -d --build
```

## 3. Test

1. Create a private test channel, add it to the `remote-jobs` folder.
2. Wait for the log line `folder 'remote-jobs': N chats (+1 -0)` (up to 15 minutes, or
   `docker compose restart`).
3. Paste one of `tests/fixtures/posts/*.txt` into it. The alert should arrive within
   seconds. Your own posts count; there is no "incoming only" filter.

## Bot commands

Send these to your bot. It answers only `NOTIFY_CHAT_ID` and ignores everyone else.

| Command | What it does |
|---|---|
| `/summary` | Last 24h: posts read and skipped, job posts, alerts, near misses (50–69), errors. `/summary 48` for another window |
| `/top` | Best job posts of the last 24h that are open to Ethiopia, whatever their score. `/top 10` |
| `/status` | Connected or not, chats watched, last post read, model, summary time |
| `/scan` | Re-read the last 24h of every chat (max 72: `/scan 6`). Posts already seen are skipped, alerts arrive as usual. Takes minutes, and on the Gemini free tier it spends quota |
| `/help` | The list above |

The same summary arrives every day at `DAILY_DIGEST_AT`.

## Settings

All in `.env`; see `.env.example`. The ones you will most likely change:

| Variable | Default | Meaning |
|---|---|---|
| `FIT_THRESHOLD` | `70` | Minimum fit score for an alert |
| `QUIET_HOURS` | off | e.g. `23:00-07:00` in `TZ_NAME`; alerts are held and sent when it ends |
| `DAILY_DIGEST_AT` | off | e.g. `20:00`; the `/summary` of the last 24h, sent daily |
| `MIN_POST_CHARS` | `200` | Shorter posts never reach the API |
| `BACKFILL_PER_CHANNEL` | `0` | First start only: score the last N posts per channel |
| `LLM_PROVIDER` | key that is set | `gemini` or `anthropic` |
| `LLM_MODEL` | per provider | `gemini-3.5-flash-lite` or `claude-haiku-5-5` |
| `CLAUDE_EFFORT` | `medium` | Claude only: `low` is cheaper, `high` thinks harder |

Cost: Gemini's free tier costs nothing but has per-minute and per-day request limits
(see https://aistudio.google.com/rate-limit). The client retries a 429 for about two
minutes; posts that still fail are recorded and listed in the daily digest, if
`DAILY_DIGEST_AT` is set. On Claude, Haiku 5.5 is $0.10 / $0.50 per million input /
output tokens with the resume prompt cached: roughly a tenth of a cent per scored post
(an estimate). Short posts and ads never reach either API.

## Privacy

The resume, preferences and every scored post are sent to the provider you choose.

- **Gemini free tier:** Google's terms for unpaid services allow it to use prompts and
  responses to improve its products, and human reviewers may read them (after they are
  disconnected from your account). Google advises against sending personal information
  to unpaid services. Keep `config/resume.md` free of contact details, and enable
  billing on the project if that matters to you: paid use is not used for training.
- **Claude API:** inputs and outputs are not used for training by default.

## Security

- `TELEGRAM_SESSION_STRING` is full access to your Telegram account. Keep `.env` at
  mode 600 and never commit it. If it leaks: Telegram → Settings → Devices → terminate
  the `job-watcher` session, then run `gen_session.py` again.
- The bot token never appears in logs (httpx request logging is turned off).
- Resume and post text go only to the model provider you configure; see [Privacy](#privacy).
- The container runs as `WATCHER_UID` (your user), read-only root filesystem, all capabilities dropped,
  256 MB memory cap, logs capped at 30 MB.
- Telegram's terms on automating user accounts: this design only reads, through the
  official MTProto API, at human-scale volume. Read the current terms yourself before
  relying on it.
