# Wyze timelapse

Takes a picture from each Wyze camera at set times of day and turns the pictures into timelapse videos:
one per day, week, month and year. It runs by itself on a Mac and tells you when something goes wrong.

## Setup

You need a Mac (macOS 12 or newer), [Docker Desktop](https://www.docker.com/products/docker-desktop/)
opened once, and a Wyze camera with RTSP turned on (Wyze app: camera Settings, Advanced Settings, RTSP).

1. Put this folder somewhere it can stay. If you move it later, run step 2 again.
2. In Terminal, from this folder, run `./launchd/install.sh`
3. Open <http://localhost:8080>. Enter each camera's address, press **Test cameras** to see its picture,
   then **Save settings**.

From then on it runs by itself. The settings page shows each camera's latest picture, and changes made
there apply within seconds.

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

To use other folders, such as an external drive: run `open -e .env`, change `IMAGES_PATH` and
`VIDEOS_PATH` to folders that exist, and run `./launchd/install.sh` again.

Once a night, yesterday's video is built. When a week, month or year has just ended, the shorter videos
are joined into the longer one without losing quality. After nights when the Mac was off, the next run
catches up.

Weeks run Monday to Sunday. A week that crosses a month end belongs to the month its Thursday is in, so a
monthly video can start or end up to three days off the calendar month. No day is left out or shown twice.

## Alerts

| | |
| --- | --- |
| On the Mac | Any failure shows a notification. Details are in the `logs` folder. |
| On a phone | Put an [ntfy.sh](https://ntfy.sh) topic address in `NOTIFY_URL` in `.env`. |
| When it stops running | Put a [healthchecks.io](https://healthchecks.io) check address (period: 1 day) in `HEARTBEAT_URL` in `.env`. It alerts you when the Mac is off, logged out or otherwise silent. |

Edit `.env` with `open -e .env`. No need to run anything afterwards.

## When something goes wrong

| Message | What to do |
| --- | --- |
| `no usable frame after 3 attempts` | The camera is off, has a new address, or its password changed. Check it with **Test cameras**. |
| `only 1 of 3 snapshots were taken` | The Mac was asleep or off, or the camera was unreachable. Nothing to repair. |
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

`app/rtsp_grabber.py` is a rewrite of [jcriger/rtsp-grabber](https://github.com/jcriger/rtsp-grabber) (MIT).
