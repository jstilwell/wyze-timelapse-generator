# Wyze timelapse generator

A Mac-only tool for a non-technical user. launchd starts short-lived Docker jobs that grab one RTSP
snapshot per camera and stitch finished days into videos; a localhost page edits the config.

## Always

- **Keep `README.md` up to date in the same commit as any change** to behaviour, setup, commands, settings
  or error messages. The README is terse and readable first and foremost: plain words, short sentences,
  written for the person using the tool. No implementation detail, which belongs in code comments.
- Run `./test.sh` before every commit. It needs only Docker.
- Check settings-page changes in a real browser, not only with tests (see "Trying the page").

## Layout

| Path | What it is |
| --- | --- |
| `app/timelapse.py` | `grab` and `stitch` jobs, config validation |
| `app/rtsp_grabber.py` | One frame from an RTSP stream through ffmpeg |
| `app/web.py` | Settings page: stdlib `http.server`, no JavaScript, no external assets |
| `app/test_timelapse.py` | Every test. They run inside the image, where ffmpeg is. |
| `launchd/install.sh` | Writes `.env`, starts the page, creates the LaunchAgents from `config/config.json` |
| `launchd/run.sh` | Runs one job, logs it, sends the failure notification or the heartbeat |
| `compose.yaml`, `Dockerfile` | The `grab`, `stitch` and `web` services |

`config/config.json` and `.env` are git-ignored and hold camera passwords and alert addresses.

## Rules that are easy to break

- Python standard library only, plus `piexif`. Images, packages and CI actions are pinned by digest or
  hash; Dependabot moves them.
- The page answers only as `localhost` (Host check) and accepts a POST only with a matching `Origin`. Do
  not set `Referrer-Policy: no-referrer`: browsers then send `Origin: null` and the page's own form is
  refused. Unit tests cannot see this.
- A saved camera password is never rendered, logged, or put in a notification. The page shows it masked
  and `from_form` restores it only for the exact same address.
- Request input never becomes a path without matching `NAME` first.
- Output is written to a `.part` or `.tmp` name, validated, then moved into place with `os.replace`.
- Jobs report each failure as one `PROBLEM:` line and exit non-zero; `run.sh` turns those into alerts.
- Containers run read-only, as `nobody`, with no capabilities. `/tmp` is the only scratch space.
- A snapshot folder for today is never stitched; a failed build stops that camera's chain so no longer
  video is built over a hole.

## Trying the page

`./launchd/install.sh` installs LaunchAgents on the machine it runs on, so do not use it to test. Instead:

```sh
cp config/config.example.json config/config.json   # git-ignored; remove it afterwards
printf 'IMAGES_PATH=/tmp/wt\nVIDEOS_PATH=/tmp/wt\nTIMEZONE=UTC\nWEB_PORT=18080\n' > /tmp/wt.env && mkdir -p /tmp/wt
docker compose -p wyze-test --env-file /tmp/wt.env up -d --build web   # http://localhost:18080
docker compose -p wyze-test --env-file /tmp/wt.env down
```
