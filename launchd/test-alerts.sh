#!/bin/zsh
# Runs run.sh's real alert code in a throwaway copy of the project, with curl and the Mac notification stubbed.
# Mac only, since it needs osascript and plutil. test.sh runs it.
set -e
repo=${0:A:h:h}
tmp=$(mktemp -d)
trap 'rm -rf $tmp' EXIT
mkdir -p $tmp/launchd $tmp/config $tmp/bin
cp $repo/launchd/run.sh $tmp/launchd/
export CALLS=$tmp/calls PATH=$tmp/bin:$PATH REAL_OSASCRIPT=$(command -v osascript)

cat > $tmp/bin/curl <<'STUB'
#!/bin/zsh
conf=$(cat)
print -r -- "ARGS $*"$'\n'"CONF $conf" >> $CALLS
[[ $* != *" -T "* ]] || cat ${@[-1]} >> $CALLS  # a mail message goes as a file
[[ $conf != *pushover* ]]  # Pushover "fails", to show the others still go out
STUB
cat > $tmp/bin/osascript <<'STUB'
#!/bin/zsh
[[ $* == *"display notification"* ]] || exec $REAL_OSASCRIPT "$@"
STUB
cat > $tmp/launchd/install.sh <<'STUB'
#!/bin/zsh
print -r 'PROBLEM: camera "front" said \ no'
exit 1
STUB
chmod +x $tmp/bin/* $tmp/launchd/*.sh

expect() { grep -qF -- "$1" ${2:-$CALLS} || { print -u2 "test-alerts: no [$1] in:"; cat ${2:-$CALLS} >&2; exit 1 } }

cat > $tmp/config/config.json <<'JSON'
{"alerts": {"ntfy_url": "https://ntfy.example/topic", "telegram_token": "1:TGSECRET", "telegram_chat": "42",
 "pushover_token": "POSECRET", "pushover_user": "POUSER", "email_provider": "cloudflare", "email_key": "CFSECRET",
 "email_account": "abc", "email_from": "a@example.com", "email_to": "b@example.com"}}
JSON
$tmp/launchd/run.sh reload >/dev/null && { print -u2 "test-alerts: a failed job must stay failed"; exit 1 }
expect 'url = "https://ntfy.example/topic"'
expect 'url = "https://api.telegram.org/bot1:TGSECRET/sendMessage"'
expect 'chat_id=42'
expect 'url = "https://api.pushover.net/1/messages.json"'
expect 'data-urlencode = "user=POUSER"'
expect 'url = "https://api.cloudflare.com/client/v4/accounts/abc/email/sending/send"'
expect 'header = "Authorization: Bearer CFSECRET"'
expect '"subject":"Wyze timelapse: reload failed","text":"PROBLEM: camera \"front\" said \\ no"'
expect 'could not send the Pushover alert' $tmp/logs/reload.log
if grep '^ARGS' $CALLS | grep -E 'SECRET|POUSER'; then print -u2 "test-alerts: a key is on a command line"; exit 1; fi

: > $CALLS
sed -i '' 's/"cloudflare"/"resend"/; s/"https:[^"]*"/""/; s/"POSECRET"/""/' $tmp/config/config.json
$tmp/launchd/run.sh test >/dev/null
expect 'url = "https://api.resend.com/emails"'
expect 'Wyze timelapse: test'
if grep -q 'ntfy\|pushover' $CALLS; then print -u2 "test-alerts: a channel that is off was used"; exit 1; fi

: > $CALLS
sed -i '' 's/"resend"/"gmail"/' $tmp/config/config.json
$tmp/launchd/run.sh reload >/dev/null || true
expect 'url = "smtps://smtp.gmail.com:465"'
expect 'user = "a@example.com:CFSECRET"'
expect '--mail-from a@example.com --mail-rcpt b@example.com'
expect 'Subject: Wyze timelapse: reload failed'
expect 'PROBLEM: camera "front" said \ no'
if grep '^ARGS' $CALLS | grep SECRET; then print -u2 "test-alerts: a key is on a command line"; exit 1; fi

: > $CALLS
print '{}' > $tmp/config/config.json  # an older config, and an address still in .env, as before these settings
print 'NOTIFY_URL=https://ntfy.example/from-env' > $tmp/.env
$tmp/launchd/run.sh test >/dev/null
expect 'url = "https://ntfy.example/from-env"'
print "alert checks passed"
