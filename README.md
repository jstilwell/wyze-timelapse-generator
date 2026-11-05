# Wyze timelapse

Takes a picture from each Wyze camera a few times a day and turns the pictures into timelapse videos:
one per day, then per week, per month and per year. If anything goes wrong, the Mac shows a notification.

## What you need

- A Mac running macOS 12 or newer that is awake at the snapshot times
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), opened once so it finishes its own setup
- A Wyze camera with RTSP turned on (Wyze app: camera Settings, Advanced Settings, RTSP)

## Setup

1. Put this folder somewhere it can stay. (If you move it later, run step 2 again.)
2. In Terminal, from this folder, run:

   ```sh
   ./launchd/install.sh
   ```

   The first run creates a file named `.env`. Open it, set `IMAGES_PATH` and `VIDEOS_PATH` to two folders
   that already exist (they can be the same folder), then run the command again.
3. Open <http://localhost:8080>, enter the camera address and the times of day you want pictures, and save.
4. Check that it works:

   ```sh
   ./launchd/run.sh grab
   ```

   A picture should appear in your images folder within about ten seconds.

From then on it runs by itself. Changing the times on the settings page updates the schedule automatically.

## Where things end up

```
IMAGES_PATH/front-yard/2026-11-05/front-yard_2026-11-05_08-00-03.jpg
VIDEOS_PATH/front-yard/daily/front-yard_2026-11-05.mp4
VIDEOS_PATH/front-yard/weekly/front-yard_2026-W45.mp4
VIDEOS_PATH/front-yard/monthly/front-yard_2026-11.mp4
VIDEOS_PATH/front-yard/yearly/front-yard_2026.mp4
```

Once a night, one job builds yesterday's video. When a week, month or year has just ended it also joins the
shorter videos into the longer one, without re-compressing them. If the Mac was off for a few nights, the
next run catches up on everything it missed.

Weeks run Monday to Sunday. A week that crosses a month end belongs to the month its Thursday is in, so a
monthly video can start or end up to three days off the calendar month, and no day is ever left out or
shown twice.

## When something goes wrong

Every run is checked: each picture and each video is fully decoded before it is kept, and each night the
number of pictures taken the day before is compared with the number of times you set. A failure shows a
macOS notification and is written to the `logs` folder here.

To also get failures on a phone (yours or someone else's), put a web address in `NOTIFY_URL` in `.env`;
an [ntfy.sh](https://ntfy.sh) topic address works as it is.

## Other commands

| | |
| --- | --- |
| `./launchd/run.sh stitch` | Build any missing videos now |
| `./launchd/install.sh uninstall` | Stop everything. Pictures and videos are kept. |
| `docker compose run --rm --entrypoint python stitch -m unittest discover -s /app` | Run the self-tests |
| `docker compose run --rm --entrypoint python grab rtsp_grabber.py rtsp://ADDRESS:PORT/live /images/test.jpg` | Grab a single picture by hand |

`app/rtsp_grabber.py` is a rewrite of [jcriger/rtsp-grabber](https://github.com/jcriger/rtsp-grabber) (MIT).
