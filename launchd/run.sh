#!/bin/zsh
# usage: run.sh grab|stitch|reload
# Runs one compose job, or for "reload" re-applies the schedule after a config change. Whatever goes wrong
# (Docker down, storage missing, camera offline, corrupt file, missed snapshots, bad config) ends up in
# logs/<job>.log and in a notification.
cd "${0:A:h:h}" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:$PATH"  # launchd starts with a bare PATH
job=$1
mkdir -p logs

notify() {
  # the message is passed as an argument, never spliced into the script text
  osascript -e 'on run a' \
    -e 'display notification (item 1 of a) with title (item 2 of a) sound name "Basso"' \
    -e 'end run' -- "$1" "Wyze timelapse: $job failed"
  local url=$(sed -n 's/^NOTIFY_URL=//p' .env 2>/dev/null)
  [[ -z $url ]] || curl -fsS -m 20 -H "Title: Wyze timelapse: $job failed" -d "$1" "$url" >/dev/null
}

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
fi
exit $rc
