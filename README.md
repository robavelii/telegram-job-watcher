# telegram-job-watcher

Watches every channel in one Telegram folder (`remote-jobs`), decides whether each new
post is a job, translates it, scores it against your resume with Claude, and pings you
through a bot when it fits.

```
Telegram (your user account, MTProto, read-only)
  │ new post in a folder channel
  ▼
listener ── pre-filter (length, ad markers) ── dedupe (SQLite, exact + near-duplicate)
  │
  ▼
scorer: one Claude Haiku call → strict JSON (is job? translation, eligibility, fit 0-100)
  │ job, fit ≥ FIT_THRESHOLD, eligible from Ethiopia ≠ "no"
  ▼
notifier: Bot API sendMessage to your own chat (held during quiet hours)
```

- **Reads with your account, writes with a bot.** The user session never sends,
  marks as read, or joins anything.
- **No inbound ports.** Only outbound connections to Telegram and the Anthropic API,
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
| `watcher/scorer.py` | Prompt, JSON schema and Claude call |
| `watcher/notifier.py` | Bot API sender, quiet-hours outbox, alert and digest formatting |
| `watcher/store.py` | SQLite: seen posts, results, outbox |
| `watcher/main.py` | Entry point, reconnect loop, scheduler (heartbeat, outbox, digest) |
| `gen_session.py` | Makes the session string. Run it on your laptop |
| `config/*.example` | Templates for `resume.md` and `preferences.yaml` (both gitignored) |

## 1. On your laptop

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Get `api_id` and `api_hash` at https://my.telegram.org → API development tools, then:

```bash
.venv/bin/python gen_session.py --out session.txt
```

Scan the QR code from Telegram on your phone (Settings → Devices → Link Desktop Device).
It prints your user id, which is your `NOTIFY_CHAT_ID`. Use `--phone` instead if QR
login gives trouble. Delete `session.txt` once the string is on the server.

Run the tests:

```bash
.venv/bin/python -m pytest
```

`tests/test_live_scorer.py` sends the sample posts to the real model (a few cents) and
is skipped unless you set `RUN_LIVE=1` and `ANTHROPIC_API_KEY`.

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

## Settings

All in `.env`; see `.env.example`. The ones you will most likely change:

| Variable | Default | Meaning |
|---|---|---|
| `FIT_THRESHOLD` | `70` | Minimum fit score for an alert |
| `QUIET_HOURS` | off | e.g. `23:00-07:00` in `TZ_NAME`; alerts are held and sent when it ends |
| `DAILY_DIGEST_AT` | off | e.g. `20:00`; near misses (`NEAR_MISS_MIN`–threshold) and scoring errors |
| `MIN_POST_CHARS` | `200` | Shorter posts never reach the API |
| `BACKFILL_PER_CHANNEL` | `0` | First start only: score the last N posts per channel |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | |
| `CLAUDE_EFFORT` | `medium` | `low` is cheaper, `high` thinks harder |

Cost: Haiku 5.5 is $0.10 / $0.50 per million input / output tokens, and the resume
prompt is cached. Expect roughly a tenth of a cent per scored post (an estimate, not
measured yet). Short posts and ads cost nothing.

## Security

- `TELEGRAM_SESSION_STRING` is full access to your Telegram account. Keep `.env` at
  mode 600 and never commit it. If it leaks: Telegram → Settings → Devices → terminate
  the `job-watcher` session, then run `gen_session.py` again.
- The bot token never appears in logs (httpx request logging is turned off).
- Resume and post text go only to the Anthropic API.
- The container runs as uid 1000, read-only root filesystem, all capabilities dropped,
  256 MB memory cap, logs capped at 30 MB.
- Telegram's terms on automating user accounts: this design only reads, through the
  official MTProto API, at human-scale volume. Read the current terms yourself before
  relying on it.
