#!/usr/bin/env bash
# Add a client to the agency.
#
#   scripts/new-client.sh <x_handle> "<Display Name>" --channel C0123ABCDEF --employee U0123ABCDEF \
#       [--topics "cold email, Clay GTM"] [--watch "@peer1, @peer2"] [--tz Europe/London] [--dry-run]
#
# 1. Adds them to the scraping console roster (daily tracking) and starts a history harvest.
# 2. Appends them to clients.yaml.
# 3. Prints what's left: commit + push, invite the bot to the channel, connect X on the dashboard.
#
# Needs XCONSOLE_API_KEY in the environment or in .env.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

handle="${1:-}"; name="${2:-}"
[ -n "$handle" ] && [ -n "$name" ] || { sed -n '2,11p' "$0"; exit 1; }
shift 2
handle="$(echo "${handle#@}" | tr '[:upper:]' '[:lower:]')"
channel=""; employee=""; topics=""; watch=""; tz="UTC"; dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --channel) channel="$2"; shift 2 ;;
        --employee) employee="$2"; shift 2 ;;
        --topics) topics="$2"; shift 2 ;;
        --watch) watch="$2"; shift 2 ;;
        --tz) tz="$2"; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        *) echo "unknown option $1"; exit 1 ;;
    esac
done
[ -n "$channel" ] || { echo "--channel is required (the client's Slack channel ID)"; exit 1; }

api="${XCONSOLE_URL:-https://scraping.thought-pilot.co/api}"
: "${XCONSOLE_API_KEY:?set XCONSOLE_API_KEY (or put it in .env)}"
console() {
    curl -sS -f -X "$1" "$api$2" -H "X-API-Key: $XCONSOLE_API_KEY" \
        -H "User-Agent: perpetual-x-bot/1.0" -H "Content-Type: application/json" ${3:+-d "$3"}
}
list() {  # "a, b" -> [a, b] with each item quoted
    [ -z "$1" ] && { echo "[]"; return; }
    echo "$1" | awk -F',' '{printf "["; for (i=1;i<=NF;i++){gsub(/^ +| +$/,"",$i); printf "%s\"%s\"", (i>1?", ":""), $i} printf "]"}'
}

# ---- 1. console roster
if console GET /roster | grep -qi "\"username\":\"$handle\""; then
    echo "✓ @$handle is already on the console roster"
elif [ $dry_run = 1 ]; then
    echo "· (dry run) would add @$handle to the console roster"
else
    console POST /roster "{\"username\":\"$handle\",\"display_name\":\"$name\",\"group\":\"Agency clients\"}" >/dev/null
    console POST /jobs "{\"actor\":\"client-content\",\"input\":{\"username\":\"$handle\",\"tabs\":[\"posts\",\"replies\"],\"limit\":300,\"max_pages\":10}}" >/dev/null
    echo "✓ added @$handle to the console roster and started a 300-post history harvest"
fi

# ---- 2. clients.yaml
if grep -qE "^\s*- handle: $handle\s*$" clients.yaml; then
    echo "✓ @$handle is already in clients.yaml"
else
    block="  - handle: $handle
    name: \"$name\"
    slack_channel: $channel
    employee: ${employee:-null}
    niche_topics: $(list "$topics")
    watch_accounts: $(list "$watch")
    avoid_accounts: []
    timezone: $tz"
    if [ $dry_run = 1 ]; then
        echo "· (dry run) would add to clients.yaml:"; echo "$block"
    else
        sed -i 's/^clients: \[\]$/clients:/' clients.yaml
        printf '%s\n' "$block" >> clients.yaml
        echo "✓ added @$handle to clients.yaml"
    fi
fi

cat <<EOF

Remaining steps for @$handle
  1. In Slack: invite the Perpetual X bot to the channel (/invite @Perpetual X).
     Make sure the employee's member ID is in SLACK_ALLOWED_USERS on agency-bot.
  2. git commit -am "add @$handle" && git push   → Render redeploys agency-bot (~3 min).
     The bot creates the profile, backfills 4 weeks of numbers, learns the voice and
     says hello in the channel within a few minutes.
  3. On the dashboard, open @$handle → Connect X account (signed in to X as @$handle)
     so /post can publish approved tweets, threads and engage-back replies.
EOF
