#!/bin/sh
# Self-tests, run inside the image under the same restrictions the jobs get. Needs only Docker.
# usage: ./test.sh [unittest arguments, e.g. -v or -k Server]
set -e
cd "$(dirname "$0")"
docker build -q -t wyze-timelapse:test .
docker run --rm --read-only --tmpfs /tmp --network none --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python wyze-timelapse:test -m unittest discover -s /app -b "$@"
TIMEZONE=UTC IMAGES_PATH=/tmp VIDEOS_PATH=/tmp docker compose config -q
if command -v zsh >/dev/null; then zsh -n launchd/install.sh && zsh -n launchd/run.sh; fi
echo "all checks passed"
