#!/bin/zsh
# usage: run.sh grab|stitch|reload|test
# Runs one compose job, for "reload" re-applies the schedule after a config change, or for "test" sends a test
# message everywhere alerts are set up. Whatever goes wrong
# (Docker down, storage missing, camera offline, corrupt file, missed snapshots, bad config) ends up in
# logs/<job>.log and in a notification. A run that works pings HEARTBEAT_URL, so that silence (Mac off, logged
# out, agents unloaded) is noticed by whoever is watching that address.
cd "${0:A:h:h}" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:$PATH"  # launchd starts with a bare PATH
job=$1
mkdir -p logs

setting() { sed -n "s/^$1=//p" .env 2>/dev/null }
# shortcut: a config.json that is not valid JSON reads as empty, so only the Mac and .env's NOTIFY_URL still alert
alert() { plutil -extract alerts.$1 raw -o - config/config.json 2>/dev/null }

unsent=0
send() {  # service, curl config, then more curl options. Keys go in the config, on stdin, to stay out of `ps`.
  local service=$1 conf=$2
  shift 2
  # Sent in a job's failure path, so a service that fails is logged and the next one is still tried.
  if curl -fsg -m 20 -o /dev/null -K - "$@" <<<"$conf"; then
    print -r -- "$service alert sent"
  else
    print -r -- "could not send the $service alert (curl error $?)" | tee -a "logs/$job.log"
    unsent=1
  fi
}

notify() {
  local title="Wyze timelapse: $job failed"
  [[ $job == test ]] && title="Wyze timelapse: test"
  # the message is passed as an argument, never spliced into the script text
  osascript -e 'on run a' \
    -e 'display notification (item 1 of a) with title (item 2 of a) sound name "Basso"' \
    -e 'end run' -- "$1" "$title"

  local url=$(alert ntfy_url)
  url=${url:-$(setting NOTIFY_URL)}
  # --data-raw: plain -d would read a file if the message happened to start with @
  [[ -z $url ]] || send ntfy "url = \"$url\"" -H "Title: $title" --data-raw "$1"

  local token=$(alert telegram_token) chat=$(alert telegram_chat)
  [[ -z $token || -z $chat ]] || send Telegram "url = \"https://api.telegram.org/bot$token/sendMessage\"" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$title"$'\n'"$1"

  local app=$(alert pushover_token) user=$(alert pushover_user)
  [[ -z $app || -z $user ]] || send Pushover "url = \"https://api.pushover.net/1/messages.json\"
data-urlencode = \"token=$app\"
data-urlencode = \"user=$user\"" --data-urlencode "title=$title" --data-urlencode "message=$1"

  local provider=$(alert email_provider)
  [[ -n $provider ]] || return 0
  url=https://api.resend.com/emails
  [[ $provider == cloudflare ]] &&
    url=https://api.cloudflare.com/client/v4/accounts/$(alert email_account)/email/sending/send
  # osascript writes the JSON because every Mac has it, and neither jq nor python is certain to be there.
  # shortcut: Cloudflare can answer 200 and still list the address as bounced, which counts as sent here.
  send "$provider email" "url = \"$url\"
header = \"Authorization: Bearer $(alert email_key)\"" -H 'Content-Type: application/json' --data-raw "$(
    osascript -l JavaScript -e 'function run(a) { return JSON.stringify({from: a[0], to: a[1], subject: a[2], text: a[3]}) }' \
      -- "$(alert email_from)" "$(alert email_to)" "$title" "$1")"
}

if [[ $job == test ]]; then
  notify "This is a test. If you can read this, alerts reach you here."
  exit $unsent
fi

if [[ $job == reload ]]; then
  out=$(launchd/install.sh schedules 2>&1)
  rc=$?
else
  if ! docker info >/dev/null 2>&1; then  # Docker Desktop is not running: start it and give it 3 minutes
    open -ga Docker
    for i in {1..36}; do sleep 5; docker info >/dev/null 2>&1 && break; done
  fi
  out=$(docker compose run --rm "$job" 2>&1)
  rc=$?
fi
print -r -- "=== $(date '+%F %T') $job exit=$rc"$'\n'"$out" >> "logs/$job.log"
if (( rc )); then
  problems=$(print -r -- "$out" | grep '^PROBLEM:')
  notify "${${problems:-$out}: -400}"
elif [[ $job != reload ]]; then
  url=$(setting HEARTBEAT_URL)
  [[ -z $url ]] || curl -fsS -m 20 -o /dev/null "$url"
fi
exit $rc
