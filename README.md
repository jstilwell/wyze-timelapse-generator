# Wyze timelapse

Takes a picture from each Wyze camera at set times of day and turns the pictures into timelapse videos:
one per day, week, month and year. It runs by itself on a Mac and tells you when something goes wrong.

## Setup

You need a Mac (macOS 12 or newer), [Docker Desktop](https://www.docker.com/products/docker-desktop/)
opened once, and a Wyze camera with RTSP turned on (Wyze app: camera Settings, Advanced Settings, RTSP).
Other cameras with an `rtsp://` or `rtsps://` address work too, UniFi Protect included.

1. Download this project: on [its GitHub page](https://github.com/jstilwell/wyze-timelapse-generator), press
   **Code**, then **Download ZIP**. Unzip it somewhere it can stay. If you move it later, run step 2 again.
2. In Terminal, from this folder, run `./launchd/install.sh`
3. Open <http://localhost:8080>. Enter each camera's address, press **Test cameras** to see its picture,
   then **Save settings**.

From then on it runs by itself. The settings page shows each camera's latest picture and how the last run
went, and changes made there apply within seconds.

**Keep the Mac awake.** In System Settings, under Energy (or Battery, Options), turn on "Prevent automatic
sleeping when the display is off". A sleeping Mac takes one late picture when it wakes instead of the ones
it missed.

## Where things end up

```
~/Pictures/wyze-timelapse/front-yard/2026-11-05/front-yard_2026-11-05_08-00-03.jpg
~/Movies/wyze-timelapse/front-yard/daily/front-yard_2026-11-05.mp4
~/Movies/wyze-timelapse/front-yard/weekly/front-yard_2026-W45.mp4
~/Movies/wyze-timelapse/front-yard/monthly/front-yard_2026-11.mp4
~/Movies/wyze-timelapse/front-yard/yearly/front-yard_2026.mp4
```

A picture is named for the time its run started, so every camera's picture from one run has the same time.

To use other folders, such as an external drive: run `open -e .env`, change `IMAGES_PATH` and
`VIDEOS_PATH` to folders that exist, and run `./launchd/install.sh` again.

Once a night, a video is built for every day, week, month or year that has just ended, straight from that
period's pictures. Keep the pictures: the videos are made from them. After nights when the Mac was off, the
next run catches up. Weeks run Monday to Sunday. A year of pictures can take a while to build.

## Tuning the videos

All on the settings page:

- **Videos.** For daily, weekly, monthly and yearly videos separately: whether to make them, how fast they
  play, and which part of the day their pictures come from. A yearly video made only from pictures taken
  between 11:00 and 13:00 shows the seasons without day and night flashing past.
- **Look.** Size, shape, turning and mirroring, format, quality, flicker smoothing, blending, grain
  reduction, sharpening, brightness, contrast, colour strength, and a date stamp in the corner.
- **Preview look** shows the result on each camera's latest picture without saving anything.

Changes apply to videos built from then on. To remake a video, delete its file and run
`./launchd/run.sh stitch`, or wait for the next night.

## Checking for a blocked view (optional)

Claude, Anthropic's AI, can look at each picture as it is taken. If something close to the camera is in the
way, the picture is taken again half a minute later, up to three more times. Movement further back in the
scene is left alone. A picture is always kept, even when the check fails.

1. Get an API key at <https://platform.claude.com>.
2. Run `open -e .env` and put the key after `ANTHROPIC_API_KEY=`.
3. On the settings page, tick **Check each picture with Claude** and pick a model. Each one is listed with
   its estimated monthly cost for your cameras and picture times; **Compare the models** explains the
   differences.
4. In the box below, describe anything Claude should know, such as "the white post on the left is always
   there".

Each check sends the new picture, shrunk, to Anthropic. Sending the camera's previous picture along shows
Claude the usual view; it can be switched off to save money, and the page shows the cost both ways. The key
stays in `.env` and is never shown on the settings page.

A picture kept with its view still blocked gets an empty `.blocked` file beside it, so that it is never used
as the usual view. Leave those files where they are.

## Alerts

| | |
| --- | --- |
| On the Mac | Any failure shows a notification, and the settings page lists the last run's problems. Details are in the `logs` folder. |
| Anywhere else | Fill in any of the services below under **Alerts** on the settings page. Every one you fill in gets every failure. |
| When it stops running | Put a [healthchecks.io](https://healthchecks.io) check address (period: 1 day) in `HEARTBEAT_URL` in `.env`. It alerts you when the Mac is off, logged out or otherwise silent. |

Edit `.env` with `open -e .env`. No need to run anything afterwards.

| Service | What to fill in |
| --- | --- |
| [ntfy](https://ntfy.sh) | A topic address. Anyone who knows it can read your alerts, so make the name long. An address already in `NOTIFY_URL` in `.env` keeps working. |
| Telegram | Make a bot with [@BotFather](https://t.me/BotFather) and copy its token. Send your bot a message, then open `https://api.telegram.org/bot<token>/getUpdates` and copy the `chat` `id`. |
| [Pushover](https://pushover.net) | Your user key, and the token of an application you create there. |
| Email through [Resend](https://resend.com) | An API key, a from address on a domain you verified there, and a to address. |
| Email through [Cloudflare](https://developers.cloudflare.com/email-service/) | An API token that may send email, your account id, a from address on a domain set up for Email Sending, and a to address. |
| Email through Gmail | An [app password](https://myaccount.google.com/apppasswords), your Gmail address as the from address, and a to address. App passwords need 2-Step Verification, and work and school accounts cannot make them. |

Saved keys show as `••••`. Leave that as it is to keep a key, or empty the box to remove it. If you change the email
provider, type the new provider's key.

To try them all, run `./launchd/run.sh test` in Terminal. It says which ones worked. If an alert could not be sent
when something failed, the job's file in the `logs` folder says so.

## When something goes wrong

| Message | What to do |
| --- | --- |
| `no usable frame after 3 attempts` | The camera is off, has a new address, or its password changed. Check it with **Test cameras**. |
| `certificate verify failed` | An `rtsps://` camera is showing a different certificate than the one stored when it was added. If the camera was reset or updated, tick "store its new certificate" for it on the settings page and save. |
| `the view was still blocked after 4 pictures` | Something is staying in front of the camera: go and look. If it belongs there, say so in the notes for Claude on the settings page. |
| `the view could not be checked` | The picture was kept. The message says why: a missing or rejected `ANTHROPIC_API_KEY` in `.env`, or no internet. |
| `only 1 of 3 snapshots were taken` | The Mac was asleep or off, or the camera was unreachable. Nothing to repair. A day on which the settings were saved is not counted. |
| `has 1 frames, expected 14` | The camera's picture size changed part-way through that video. On the settings page under Look, switch off blending and flicker smoothing, save, then run `./launchd/run.sh stitch`. |
| `ran too long and was stopped` | Something hung: a camera, the storage drive or Docker. The next run starts fresh. If it keeps happening, restart Docker Desktop. |
| `… is corrupt` | Delete the file it names from the pictures or videos folder, then run `./launchd/run.sh stitch`. |
| `Cannot connect to the Docker daemon` | Open Docker Desktop. In its settings, turn on starting when you sign in. |
| `bind source path does not exist` | The pictures or videos folder is missing. Reconnect the drive, then run `./launchd/install.sh`. |
| The settings page does not open | Run `./launchd/install.sh`. |

## Commands

| | |
| --- | --- |
| `./launchd/run.sh grab` | Take a picture from every camera now |
| `./launchd/run.sh stitch` | Build any missing videos now |
| `./launchd/install.sh` | Install, or apply an update or a changed `.env` |
| `./launchd/install.sh uninstall` | Stop everything. Pictures and videos are kept. |
| `./test.sh` | Run the self-tests (needs only Docker) |

## Updates

Everything the tool is built from is pinned to an exact version. On GitHub, Dependabot proposes newer
versions once a month and the self-tests run on each proposal. After pulling an update, run
`./launchd/install.sh`.

## Credits and license

This project started from [rtsp-grabber](https://github.com/jcriger/rtsp-grabber) by jcriger, and
`app/rtsp_grabber.py` is a rewrite of it. Thank you, jcriger. Both projects are under the [MIT License](LICENSE).

This is an independent project. It is not made, supported or endorsed by Wyze.
