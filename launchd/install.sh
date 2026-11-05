#!/bin/zsh
# usage: install.sh             set everything up: settings page, then the LaunchAgents from config/config.json
#        install.sh schedules   only refresh the two schedules (what the reload agent does after a config change)
#        install.sh uninstall   unload and delete all three, stop the settings page (pictures and videos stay)
#
# Agents:  snapshot  run.sh grab at every time in "times"
#          stitch    run.sh stitch once a day at "stitch_time"; that one run builds yesterday's video and,
#                    whenever a week, month or year has just ended, the weekly, monthly and yearly ones too
#          reload    run.sh reload whenever the settings page (or you) changes config/
set -e
root=${0:A:h:h}
agents=~/Library/LaunchAgents
prefix=local.wyze-timelapse
cfg=$root/config/config.json

unload() {
  launchctl bootout gui/$UID/$prefix.$1 2>/dev/null || true
  # bootout returns before the job is gone, and bootstrap fails while it is still there
  for i in {1..50}; do launchctl print gui/$UID/$prefix.$1 >/dev/null 2>&1 || break; sleep 0.1; done
}

if [[ $1 == uninstall ]]; then
  for name in snapshot stitch reload; do unload $name; rm -f $agents/$prefix.$name.plist; done
  docker compose --project-directory $root down
  exit
fi

calendar() {  # HH:MM -> launchd calendar entry. Validated here because it is written into XML.
  [[ $1 =~ '^([01][0-9]|2[0-3]):([0-5][0-9])$' ]] || { print -u2 "invalid time in $cfg: $1"; exit 1 }
  print "<dict><key>Hour</key><integer>$((10#$match[1]))</integer><key>Minute</key><integer>$((10#$match[2]))</integer></dict>"
}

agent() {  # name, program arguments (XML), trigger (XML)
  local plist=$agents/$prefix.$1.plist
  cat > $plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$prefix.$1</string>
  <key>ProgramArguments</key><array>$2</array>
  $3
  <key>StandardOutPath</key><string>$root/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$root/logs/launchd.log</string>
</dict>
</plist>
EOF
  plutil -lint -s $plist
  unload $1
  launchctl bootstrap gui/$UID $plist
}

[[ -f $cfg ]] || cp $root/config/config.example.json $cfg
mkdir -p $agents $root/logs

if [[ $1 != schedules ]]; then  # first-time checks, then build the image and start the settings page
  command -v docker >/dev/null || { print -u2 "Docker Desktop is needed first: https://www.docker.com/products/docker-desktop/"; exit 1 }
  if [[ ! -f $root/.env ]]; then  # first run: this Mac's time zone and two folders in the home folder
    sed -e "s|^TIMEZONE=.*|TIMEZONE=${$(readlink /etc/localtime)#*zoneinfo/}|" \
        -e "s|^IMAGES_PATH=.*|IMAGES_PATH=$HOME/Pictures/wyze-timelapse|" \
        -e "s|^VIDEOS_PATH=.*|VIDEOS_PATH=$HOME/Movies/wyze-timelapse|" $root/.env.example > $root/.env
    mkdir -p ~/Pictures/wyze-timelapse ~/Movies/wyze-timelapse
    print "Pictures go to ~/Pictures/wyze-timelapse and videos to ~/Movies/wyze-timelapse."
    print "To use other folders, run:  open -e $root/.env   and then this script again."
  fi
  chmod 600 $cfg $root/.env  # camera passwords and notification addresses: this user only
  for key in IMAGES_PATH VIDEOS_PATH; do
    dir=$(sed -n "s/^$key=//p" $root/.env)
    [[ -d $dir ]] || { print -u2 "$key in $root/.env is '$dir', which is not an existing folder. Fix it and run this again."; exit 1 }
  done
  docker compose --project-directory $root up -d --build
fi

times=""
for t in ${(s:,:)$(plutil -extract times json -o - $cfg | tr -d '[]" \n')}; do times+=$(calendar $t); done
[[ -n $times ]] || { print -u2 "no snapshot times in $cfg"; exit 1 }
stitch=$(calendar "$(plutil -extract stitch_time raw -o - $cfg)")

agent snapshot "<string>$root/launchd/run.sh</string><string>grab</string>" \
  "<key>StartCalendarInterval</key><array>$times</array>"
agent stitch "<string>$root/launchd/run.sh</string><string>stitch</string>" \
  "<key>StartCalendarInterval</key>$stitch"
# The reload agent is the one running this script in "schedules" mode, so it must not reload itself.
[[ $1 == schedules ]] || agent reload "<string>$root/launchd/run.sh</string><string>reload</string>" \
  "<key>WatchPaths</key><array><string>$root/config</string></array>"

[[ $1 == schedules ]] || print "\nDone. Settings page: http://localhost:$(sed -n 's/^WEB_PORT=//p' $root/.env)"
