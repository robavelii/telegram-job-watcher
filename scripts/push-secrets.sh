#!/usr/bin/env bash
# Asks for the watcher's secrets on this machine and writes them into the server's .env
# over SSH. Values go through ssh's stdin, never the command line, and are not echoed.
#
#   scripts/push-secrets.sh user@server [ssh-key] [remote-app-dir]
#   scripts/push-secrets.sh me@203.0.113.10 ~/.ssh/id_ed25519 telegram-job-watcher
#
# Press Enter at any prompt to keep the value already on the server.
# If ./session.txt exists (from `gen_session.py --out session.txt`) it is used for
# TELEGRAM_SESSION_STRING and deleted afterwards.
set -euo pipefail

HOST=${1:?usage: push-secrets.sh user@host [ssh-key] [remote-app-dir]}
KEY=${2:-}
# Relative paths are relative to the remote user's home directory.
DIR=${3:-telegram-job-watcher}
SSH=(ssh -o BatchMode=yes)
[[ -n $KEY ]] && SSH+=(-i "$KEY")

payload=""
add() { payload+="$1=$2"$'\n'; }

ask() { # name, prompt, regex, hidden(0/1)
  local name=$1 prompt=$2 pattern=$3 hidden=$4 value
  while true; do
    if [[ $hidden == 1 ]]; then read -rsp "$prompt (hidden, Enter = keep): " value; echo
    else read -rp "$prompt (Enter = keep): " value; fi
    value=${value//[$'\t\r\n ']/}
    [[ -z $value ]] && return 0
    if [[ $value =~ $pattern ]]; then add "$name" "$value"; return 0; fi
    echo "  that does not look right, try again"
  done
}

ask_text() { # name, prompt: free text such as a country name; spaces kept, ends trimmed
  local name=$1 prompt=$2 value
  read -rp "$prompt (Enter = keep): " value
  value=${value//[$'\t\r\n']/}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  [[ -n $value ]] && add "$name" "$value"
  return 0
}

echo "Writing to $HOST:$DIR/.env"
ask TELEGRAM_API_ID   "Telegram api_id (my.telegram.org)" '^[0-9]+$' 0
ask TELEGRAM_API_HASH "Telegram api_hash"                 '^[0-9a-f]{32}$' 1

session_file=""
if [[ -f session.txt ]]; then
  session_file=session.txt
  value=$(tr -d '[:space:]' < session.txt)
  [[ -n $value ]] && add TELEGRAM_SESSION_STRING "$value" && echo "Telegram session string: read from ./session.txt"
else
  ask TELEGRAM_SESSION_STRING "Telegram session string (gen_session.py)" '^[A-Za-z0-9_=+/-]{100,}$' 1
fi

ask BOT_TOKEN         "Bot token (@BotFather)"            '^[0-9]+:[A-Za-z0-9_-]{30,}$' 1
ask NOTIFY_CHAT_ID    "Your Telegram user id"             '^-?[0-9]+$' 0
ask_text CANDIDATE_LOCATION "Country you work from (e.g. Kenya)"
provider=""
while [[ -z $provider ]]; do
  read -rp "Model provider, gemini or anthropic (Enter = gemini): " provider
  provider=${provider:-gemini}
  [[ $provider == gemini || $provider == anthropic ]] || { echo "  type gemini or anthropic"; provider=""; }
done
add LLM_PROVIDER "$provider"
if [[ $provider == gemini ]]; then
  ask GEMINI_API_KEY  "Gemini API key (aistudio.google.com/apikey)" '^[A-Za-z0-9_.-]{30,}$' 1
else
  ask ANTHROPIC_API_KEY "Anthropic API key"                 '^sk-ant-[A-Za-z0-9_-]{20,}$' 1
fi

if [[ -z $payload ]]; then echo "Nothing to change."; exit 0; fi

# Runs on the server: replace or append KEY=VALUE lines read from stdin, keep mode 600.
read -r -d '' REMOTE <<'PY' || true
import os, re, sys
path = sys.argv[1]
new = dict(l.split("=", 1) for l in sys.stdin.read().splitlines() if "=" in l)
out, seen = [], set()
for line in open(path).read().splitlines():
    m = re.match(r"^([A-Z_]+)=", line)
    if m and m.group(1) in new:
        out.append(f"{m.group(1)}={new[m.group(1)]}")
        seen.add(m.group(1))
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in new.items() if k not in seen]
tmp = path + ".tmp"
with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.write("\n".join(out) + "\n")
os.replace(tmp, path)
print("updated on server:", ", ".join(sorted(new)))
PY

b64=$(printf '%s' "$REMOTE" | base64 -w0)
printf '%s' "$payload" | "${SSH[@]}" "$HOST" "python3 -c \"\$(echo $b64 | base64 -d)\" '$DIR/.env'"
payload=""

if [[ -n $session_file ]]; then
  shred -u "$session_file" 2>/dev/null || rm -f "$session_file"
  echo "Deleted ./session.txt"
fi
echo "Done. Clear your terminal scrollback if anything was pasted visibly."
