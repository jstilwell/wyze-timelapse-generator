# Wyze timelapse generator

A Mac-only tool for a non-technical user. launchd starts short-lived Docker jobs that grab one RTSP
snapshot per camera and stitch finished days into videos; a localhost page edits the config.

## Always

- **Keep `README.md` up to date in the same commit as any change** to behaviour, setup, commands, settings
  or error messages. The README is terse and readable first and foremost: plain words, short sentences,
  written for the person using the tool. No implementation detail, which belongs in code comments.
- Run `./test.sh` before every commit. It needs only Docker.
- **Before committing any change under `app/`, check for new Claude models and prices.** Read
  <https://platform.claude.com/docs/en/about-claude/models/overview> and
  <https://platform.claude.com/docs/en/about-claude/pricing>. If a current Opus, Sonnet or Haiku is not in
  `VIEW_MODELS` (`app/timelapse.py`), or a price there is out of date, then in the same commit:
  - update `VIEW_MODELS` (one model per tier, newest first within the tier order Opus, Sonnet, Haiku; the
    first entry is the default) and `PRICES_AS_OF`;
  - write its "What to expect" line in `EXPECT` (`app/web.py`). The dropdown, the cost estimates and the
    model comparison dialog are all built from those two tables, and the tests fail if they disagree;
  - check the model's API differences and adjust `app/view.py`: `NO_FALLBACK` lists models without a
    server-side refusal fallback, and the dialog's closing paragraph describes that difference;
  - keep a retired model's id working for configs that still name it, or have `with_defaults` move them on.
- Check settings-page changes in a real browser, not only with tests (see "Trying the page").

## Layout

| Path | What it is |
| --- | --- |
| `app/timelapse.py` | `grab` and `stitch` jobs, the video look, config validation and defaults |
| `app/rtsp_grabber.py` | One frame from an RTSP stream through ffmpeg |
| `app/view.py` | The optional Claude check for a blocked view (the only code that calls the Claude API) |
| `app/web.py` | Settings page: stdlib `http.server`, no external assets, and one line of JavaScript (it opens the model comparison dialog; its hash is in the CSP) |
| `app/test_timelapse.py` | Every test. They run inside the image, where ffmpeg is. |
| `launchd/install.sh` | Writes `.env`, starts the page, creates the LaunchAgents from `config/config.json` |
| `launchd/run.sh` | Runs one job, logs it, sends the failure notification or the heartbeat |
| `compose.yaml`, `Dockerfile` | The `grab`, `stitch` and `web` services |

`config/config.json` and `.env` are git-ignored and hold camera passwords and alert addresses.

## Rules that are easy to break

- Python standard library only, plus `piexif`, the `anthropic` SDK (used by `view.py` alone), and the DejaVu
  font package for the date stamp. Images, packages and CI actions are pinned by digest, hash or version;
  Dependabot moves all but the font. After changing `requirements.in`, regenerate the lock with
  `uv pip compile requirements.in --generate-hashes --universal --python-version 3.13 --no-header -o requirements.txt`.
- Claude is called only through the official SDK, never raw HTTP. `ANTHROPIC_API_KEY` goes to the grab
  container only; the settings page must never receive or show it. A failed or refused check never costs a
  picture: `snapshot()` saves one regardless and returns a note for the alert.
- The page answers only as `localhost` (Host check) and accepts a POST only with a matching `Origin`. Do
  not set `Referrer-Policy: no-referrer`: browsers then send `Origin: null` and the page's own form is
  refused. Unit tests cannot see this.
- A saved camera password is never rendered, logged, or put in a notification. The page shows it masked
  and `from_form` restores it only for the exact same address.
- Request input never becomes a path without matching `NAME` first.
- An `rtsps://` camera is always verified. Its certificate is stored when the camera is saved (trust on
  first use, `pin` in `web.py`) and ffmpeg accepts only that one (`grab_frame`). Never turn verification off.
- Output is written to a `.part` or `.tmp` name, validated, then moved into place with `os.replace`.
- Jobs report each failure as one `PROBLEM:` line and exit non-zero; `run.sh` turns those into alerts.
- Containers run read-only, as `nobody`, with no capabilities. `/tmp` is the only scratch space.
- Every video is encoded straight from the pictures of its own period, never joined from shorter videos,
  and only once that period has ended. One video failing does not stop the others.
- The look of the videos is set only through fixed choices, switches and bounded numbers (`CHOICES`,
  `SWITCHES`, `RANGES` in `timelapse.py`), and `look()` assembles fixed filter text from them. Do not add a
  free-text ffmpeg field: that was decided against on 2026-11-05. A new option needs a test that builds a
  video with it.

## Trying the page

`./launchd/install.sh` installs LaunchAgents on the machine it runs on, so do not use it to test. Instead:

```sh
cp config/config.example.json config/config.json   # git-ignored; remove it afterwards
printf 'IMAGES_PATH=/tmp/wt\nVIDEOS_PATH=/tmp/wt\nTIMEZONE=UTC\nWEB_PORT=18080\n' > /tmp/wt.env && mkdir -p /tmp/wt
docker compose -p wyze-test --env-file /tmp/wt.env up -d --build web   # http://localhost:18080
docker compose -p wyze-test --env-file /tmp/wt.env down
```
