"""Wyze timelapse jobs.

grab   - one validated, EXIF-tagged snapshot per camera
stitch - build the daily, weekly, monthly and yearly videos whose period has ended, each straight from the pictures
"""
import calendar
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import piexif

from rtsp_grabber import GrabError, grab_frame

CONFIG = Path("/config/config.json")
IMAGES, VIDEOS = Path("/images"), Path("/videos")
LEVELS = ["daily", "weekly", "monthly", "yearly"]
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
SHOT = re.compile(r"[a-z0-9_-]+_(\d{4})-(0[1-9]|1[0-2])-(\d\d)_(\d\d)-(\d\d)-\d\d\.jpg")  # the names snapshot() gives
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # installed by the Dockerfile

# The look of the videos. Each setting is a fixed choice (the first is the default), a switch or a bounded
# number, so nothing typed on the settings page ever reaches ffmpeg as text.
CHOICES = {
    "size": ("original", "2160", "1080", "720"),  # height in lines; never enlarges
    "shape": ("original", "16:9", "4:3", "1:1", "9:16"),  # cut from the middle of the picture
    "turn": ("none", "right", "upside-down", "left"),
    "format": ("h264", "h265"),
    "quality": ("high", "medium", "small"),
    "blend": ("off", "light", "strong"),
    "stamp": ("off", "date", "datetime"),
}
SWITCHES = ("mirror", "deflicker", "denoise", "sharpen")
RANGES = {"brightness": (-50, 50), "contrast": (50, 200), "saturation": (0, 300)}  # 0, 100 and 100 change nothing
VIDEO = {**{key: options[0] for key, options in CHOICES.items()}, **dict.fromkeys(SWITCHES, False),
         "brightness": 0, "contrast": 100, "saturation": 100}
LEVEL = {"make": True, "fps": 24, "from": "00:00", "to": "23:59"}  # per kind of video: speed and time-of-day window
RETAKES, RETAKE_WAIT = 3, 30  # a blocked view gets this many more pictures, this many seconds apart
# Claude models offered for the blocked-view check (the first is the default): name on the page, then dollars per
# million tokens in and out, copied from https://platform.claude.com/docs/en/about-claude/pricing. CLAUDE.md says
# when and how to bring this list up to date.
PRICES_AS_OF = "November 2026"
VIEW_MODELS = {"claude-opus-5-5": ("Claude Opus 5.5", 4.00, 20.00),
               "claude-sonnet-5-5": ("Claude Sonnet 5.5", 2.00, 10.00),
               "claude-haiku-5-5": ("Claude Haiku 5.5", 0.10, 0.50)}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
URL = re.compile(r"rtsps?://\S+")


def with_defaults(cfg):
    """Fill in what an older or hand-written config leaves out. A top-level `fps` is the old single speed."""
    if isinstance(cfg, dict) and isinstance(cfg.get("video", {}), dict) and isinstance(cfg.get("levels", {}), dict):
        fps = cfg.pop("fps", LEVEL["fps"])
        cfg["video"] = {**VIDEO, **cfg.get("video", {})}
        cfg.setdefault("check_view", False)
        cfg.setdefault("view_notes", "")
        cfg.setdefault("view_model", next(iter(VIEW_MODELS)))
        cfg.setdefault("view_compare", True)
        given = cfg.get("levels", {})
        cfg["levels"] = {}
        for level in LEVELS:
            own = given.get(level, {})
            cfg["levels"][level] = {**LEVEL, "fps": fps, **own} if isinstance(own, dict) else own
    return cfg


def read_config():
    path = CONFIG if CONFIG.exists() else CONFIG.with_name("config.example.json")
    return with_defaults(json.loads(path.read_text()))


def well_formed(cfg):
    """True when every setting is the right kind of value, so validate() and the settings page can read it."""
    return (isinstance(cfg, dict) and isinstance(cfg.get("stitch_time"), str) and isinstance(cfg.get("video"), dict)
            and isinstance(cfg.get("view_notes"), str) and isinstance(cfg.get("view_model"), str)
            and isinstance(cfg.get("levels"), dict)
            and all(isinstance(cfg["levels"].get(level), dict) and isinstance(cfg["levels"][level].get("from"), str)
                    and isinstance(cfg["levels"][level].get("to"), str) for level in LEVELS)
            and isinstance(cfg.get("cameras"), list) and isinstance(cfg.get("times"), list)
            and all(isinstance(c, dict) and all(isinstance(c.get(key, default), str) for key, default in
                                                (("name", None), ("url", None), ("cert", ""), ("cert_host", "")))
                    for c in cfg["cameras"])
            and all(isinstance(t, str) for t in cfg["times"]))


def validate(cfg):
    """Problems with a config (empty list = valid). Camera names become directory names, so they are strict."""
    if not well_formed(cfg):  # a hand-edited file can hold anything
        return ["a setting is missing or the wrong kind of value; compare config.json with config.example.json"]
    errs = []
    cams, times = cfg["cameras"], cfg["times"]
    names = [c["name"] for c in cams]
    if not cams:
        errs.append("at least one camera is required")
    if len(set(names)) != len(names):
        errs.append("camera names must be unique")
    for c in cams:
        if not NAME.fullmatch(c["name"]):
            errs.append(f"camera name {c['name']!r}: use a-z, 0-9, - and _ (max 32 characters)")
        if not URL.fullmatch(c["url"]):
            errs.append(f"camera {c['name']!r}: address must look like rtsp://address:port/path")
        elif "@" in c["url"] and "@" not in c["url"].split("/")[2]:  # the page could not hide that password
            errs.append(f"camera {c['name']!r}: write a / in the password as %2F")
    if not times or len(set(times)) != len(times) or not all(HHMM.fullmatch(t) for t in times):
        errs.append("snapshot times: one or more different 24-hour HH:MM values")
    if not HHMM.fullmatch(cfg["stitch_time"]):
        errs.append("video build time: a 24-hour HH:MM value")
    def whole(value, lo, hi):
        return type(value) is int and lo <= value <= hi

    if not whole(cfg.get("jpeg_quality"), 1, 100):
        errs.append("picture quality: a whole number from 1 to 100")
    if type(cfg.get("check_view")) is not bool or type(cfg.get("view_compare")) is not bool:
        errs.append("check each picture, and its comparison picture: on or off")
    if cfg["view_model"] not in VIEW_MODELS:
        errs.append(f"model for the check: one of {', '.join(VIEW_MODELS)}")
    if len(cfg["view_notes"]) > 2000:
        errs.append("notes for Claude: 2,000 characters at most")
    for level in LEVELS:
        own = cfg["levels"][level]
        if type(own.get("make")) is not bool or not whole(own.get("fps"), 1, 60):
            errs.append(f"{level} videos: pictures per second is a whole number from 1 to 60")
        window = own.get("from"), own.get("to")
        if not (all(isinstance(t, str) and HHMM.fullmatch(t) for t in window) and window[0] <= window[1]):
            errs.append(f"{level} videos: the picture window needs a From and a later To, as 24-hour HH:MM")
    video = cfg["video"]
    errs += [f"video {key}: one of {', '.join(options)}" for key, options in CHOICES.items()
             if not isinstance(video.get(key), str) or video[key] not in options]
    errs += [f"video {key}: on or off" for key in SWITCHES if type(video.get(key)) is not bool]
    errs += [f"video {key}: a whole number from {lo} to {hi}" for key, (lo, hi) in RANGES.items()
             if not whole(video.get(key), lo, hi)]
    return errs


def probe(src):
    """Decode every frame of an image or video (a path, or JPEG bytes) and return its frame count, width and
    height. Raises ValueError on any decoder complaint."""
    data, name = (src, "captured frame") if isinstance(src, bytes) else (None, src.name)
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=nb_read_frames,width,height", "-of", "json", "-" if data else str(src)],
        input=data, capture_output=True)
    complaint = r.stderr.decode(errors="replace").strip()
    if r.returncode or complaint:
        raise ValueError(f"{name} is corrupt: {complaint[-300:] or 'ffprobe failed'}")
    stream = (json.loads(r.stdout).get("streams") or [{}])[0]
    info = {key: int(stream.get(key) or 0) for key in ("nb_read_frames", "width", "height")}
    if not all(info.values()):
        raise ValueError(f"{name} is corrupt: no decodable video frames")
    return info


# ---- grab ----------------------------------------------------------------------------------------------------------

def latest(cam, images, clear=False):
    """Newest saved picture of a camera, or None; with `clear`, the newest that was not kept with its view
    blocked. `cam` may come from a web request, so it is checked before it becomes a path."""
    if NAME.fullmatch(cam):
        for day in sorted((images / cam).glob("????-??-??"), reverse=True):
            shots = sorted(p for p in day.glob(f"{cam}_*.jpg") if SHOT.fullmatch(p.name)
                           and not (clear and p.with_suffix(".blocked").exists()))
            if shots:
                return shots[-1]


def frame(grab, cam, quality):
    """One decodable JPEG from the camera and its size, within three tries."""
    for attempt in range(3):
        try:
            jpeg = grab(cam["url"], quality, cert=cam.get("cert"), cert_host=cam.get("cert_host"))
            return jpeg, probe(jpeg)
        except (GrabError, ValueError) as e:
            error = e
            time.sleep(2)
    raise RuntimeError(f"no usable frame after 3 attempts: {error}")


def snapshot(grab, cam, quality, images, check=None, when=None):
    """Save one picture and return (its path, a note for the alert or None).

    `when` is the moment the run started. The folder and file are named for it, so that a slow camera or a
    retake cannot push a picture out of a video's time window or into the next day.

    `check(jpeg, camera name, usual picture)` says what is blocking the view ("" for nothing). A blocked picture
    is taken again; whatever happens after the first good frame, a picture is saved, and the note says so when
    the check failed or the view never cleared."""
    jpeg, size = frame(grab, cam, quality)
    note, reason = None, ""  # `reason` ends up as what blocks the view in the picture that is saved
    for retake in range(RETAKES + 1) if check else ():
        try:
            reason = check(jpeg, cam["name"], latest(cam["name"], images, clear=True))
        except Exception as e:  # no key, no network, an API fault: the picture matters more than the check
            note, reason = f"the view could not be checked ({e}); the picture was kept", ""
            break
        if not reason:
            break
        print(f"{cam['name']}: view blocked ({reason})")
        if retake == RETAKES:
            note = f"the view was still blocked after {RETAKES + 1} pictures ({reason}); the last one was kept"
            break
        time.sleep(RETAKE_WAIT)
        try:
            jpeg, size = frame(grab, cam, quality)
        except RuntimeError as e:
            note = f"the view was blocked ({reason}) and the retake failed ({e}); the blocked picture was kept"
            break

    now = datetime.now().astimezone()  # the EXIF data keeps the moment the picture was really taken
    when = when or now
    stamp, off = now.strftime("%Y:%m:%d %H:%M:%S"), now.strftime("%z")
    off = f"{off[:3]}:{off[3:]}"
    # The RTSP URL is deliberately left out: it usually carries the camera password.
    exif = piexif.dump({
        "0th": {
            piexif.ImageIFD.Make: "Wyze",
            piexif.ImageIFD.Model: "Wyze Cam",
            piexif.ImageIFD.ImageDescription: f"Timelapse snapshot from camera {cam['name']}",
            piexif.ImageIFD.Software: "wyze-timelapse",
            piexif.ImageIFD.DateTime: stamp,
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
            piexif.ExifIFD.OffsetTime: off,
            piexif.ExifIFD.OffsetTimeOriginal: off,
            piexif.ExifIFD.OffsetTimeDigitized: off,
            piexif.ExifIFD.PixelXDimension: size["width"],
            piexif.ExifIFD.PixelYDimension: size["height"],
        },
    })
    tagged = io.BytesIO()
    piexif.insert(exif, jpeg, tagged)

    day = images / cam["name"] / f"{when:%Y-%m-%d}"
    day.mkdir(parents=True, exist_ok=True)
    final = day / f"{cam['name']}_{when:%Y-%m-%d_%H-%M-%S}.jpg"
    part = final.with_suffix(".part")
    try:
        part.write_bytes(tagged.getvalue())
        # Re-check the file as it actually landed on the mount before it gets a name the stitcher picks up.
        probe(part)
        if piexif.load(str(part))["Exif"].get(piexif.ExifIFD.DateTimeOriginal) != stamp.encode():
            raise RuntimeError("snapshot metadata did not survive the write")
        os.replace(part, final)
        if reason:  # marked, so that the next check is not shown a blocked view as the usual one
            final.with_suffix(".blocked").touch()
    finally:
        part.unlink(missing_ok=True)
    return final, note


def grab(cfg, images=IMAGES):
    started = datetime.now().astimezone()
    check = None
    if cfg["check_view"]:
        import view  # the Claude SDK is only loaded when the check is switched on

        def check(jpeg, camera, usual):
            return view.blocked(jpeg, camera, usual if cfg["view_compare"] else None, cfg["view_notes"],
                                cfg["view_model"])

    def one(cam):
        try:
            saved, note = snapshot(grab_frame, cam, cfg["jpeg_quality"], images, check, started)
            print("saved", saved)
            return note and f"{cam['name']}: {note}"
        except Exception as e:
            return f"{cam['name']}: {e}"

    with ThreadPoolExecutor(len(cfg["cameras"])) as pool:  # all at once: a dead camera must not delay the others
        return [problem for problem in pool.map(one, cfg["cameras"]) if problem]


# ---- stitch --------------------------------------------------------------------------------------------------------

def bucket(level, d):
    """Name of the video that date `d` belongs to at `level`. Weeks are ISO weeks (Monday to Sunday)."""
    y, w, _ = d.isocalendar()
    return {"daily": d.isoformat(), "weekly": f"{y}-W{w:02}", "monthly": f"{d:%Y-%m}", "yearly": f"{d:%Y}"}[level]


def look(video, still=False):
    """The ffmpeg filter chain for the chosen look, made only of fixed pieces picked by validated settings.
    `still` leaves out the two filters that work across neighbouring frames (for the settings page preview)."""
    chain = list({"none": [], "right": ["transpose=1"], "upside-down": ["hflip", "vflip"],
                  "left": ["transpose=2"]}[video["turn"]])
    if video["mirror"]:
        chain.append("hflip")
    if video["shape"] != "original":
        w, h = video["shape"].split(":")
        chain.append(f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})'")
    # shortcut: nothing evens out pictures of different sizes. When a camera's resolution changes part-way through
    # a video, ffmpeg stretches the later pictures to the first one's frame, and with Blend or Deflicker on it
    # drops frames, so build() refuses the result. Upgrade if cameras change resolution more than once in a while.
    height = "ih" if video["size"] == "original" else f"min(ih,{video['size']})"
    chain.append(f"scale=-2:'2*trunc({height}/2)':flags=lanczos")  # the encoders need even sizes
    if video["deflicker"] and not still:
        chain.append("deflicker=mode=pm:size=10")
    if video["denoise"]:
        chain.append("hqdn3d")
    if video["blend"] != "off" and not still:
        # tmix drops the per-frame stamp text; laying its output over the untouched frames keeps it
        frames = {"light": 3, "strong": 6}[video["blend"]]
        chain.append(f"split[sharp][mix];[mix]tmix=frames={frames}[mixed];[sharp][mixed]overlay")
    tone = [video[key] / 100 for key in ("brightness", "contrast", "saturation")]
    if tone != [0, 1, 1]:
        chain.append("eq=brightness={}:contrast={}:saturation={}".format(*tone))
    if video["sharpen"]:
        chain.append("unsharp=5:5:0.8")
    if video["stamp"] != "off":
        chain.append(f"drawtext=fontfile={FONT}:text='%{{metadata\\:stamp}}':fontsize=h/30:fontcolor=white"
                     ":borderw=2:bordercolor=black@0.7:x=h/40:y=h-th-h/40")
    return ",".join(chain)


def encode(shots, fps, video, output, still=False):
    """Run ffmpeg over the pictures `shots` (SHOT-named paths, in order) with the look applied. `output` is the
    encoder and destination arguments. Each picture carries its date as text for the stamp to print."""
    lines = ["ffconcat version 1.0"]
    for shot in shots:
        year, month, day, hour, minute = SHOT.fullmatch(shot.name).groups()
        text = f"{int(day)} {calendar.month_abbr[int(month)]} {year}" + f" {hour}:{minute}" * (video["stamp"] == "datetime")
        lines += [f"file '{shot}'", f"file_packet_meta stamp '{text}'"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
        listing.write("\n".join(lines) + "\n")
        listing.flush()
        # -r as an input option: one frame per picture at exactly this speed, whatever the files' own timing says
        return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-r", str(fps), "-f", "concat", "-safe", "0",
                               "-i", listing.name, "-vf", look(video, still), *output], capture_output=True)


def still(shot, video):
    """One picture with the look applied, as JPEG bytes no taller than 720 lines: the settings page preview."""
    small = {**video, "size": "720"}
    r = encode([shot], 1, small, ["-frames:v", "1", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "-"], still=True)
    if r.returncode or not r.stdout:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg produced no picture")
    return r.stdout


def build(shots, out, fps, video, title):
    """Encode the pictures into `out`, fully decode the result, then move it into place."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part")
    quality = ("high", "medium", "small").index(video["quality"])
    codec = {"h264": ["-c:v", "libx264", "-crf", ("18", "23", "28")[quality]],
             # hvc1 is the tag Apple players expect; x265 chats on stderr unless told not to
             "h265": ["-c:v", "libx265", "-crf", ("22", "26", "30")[quality], "-tag:v", "hvc1",
                      "-x265-params", "log-level=error"]}[video["format"]]
    r = encode(shots, fps, video, [
        *codec, "-pix_fmt", "yuv420p", "-metadata", f"title={title}",
        "-metadata", f"comment={len(shots)} snapshots at {fps} fps",
        "-metadata", f"creation_time={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        "-movflags", "+faststart", "-f", "mp4", str(part)])
    try:
        why = r.stderr.decode(errors="replace").strip()[-300:]
        if r.returncode or why:
            for shot in shots:  # name the damaged file, so the person reading the alert knows what to delete
                try:
                    probe(shot)
                except ValueError as e:
                    why = str(e)
                    break
            raise RuntimeError(f"ffmpeg failed building {out.name}: {why}")
        frames = probe(part)["nb_read_frames"]
        if frames != len(shots):
            raise RuntimeError(f"{out.name} has {frames} frames, expected {len(shots)}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)


def stitch(cfg, today, images=IMAGES, videos=VIDEOS, config=CONFIG):
    """Build every missing video whose period has ended. Safe to re-run: it catches up on anything a missed run
    left, and a video that fails is reported and tried again next time without holding up the others."""
    problems = []
    yesterday = today - timedelta(days=1)
    # Yesterday's pictures are counted against the picture times as they are now, so not when those may have
    # changed since: the count would be a false alarm.
    settled = not config.exists() or date.fromtimestamp(config.stat().st_mtime) < yesterday
    yesterday = yesterday.isoformat()
    for cam in (c["name"] for c in cfg["cameras"]):
        days = sorted(d for d in (images / cam).glob("????-??-??") if d.name < today.isoformat())
        for level in LEVELS:
            own = cfg["levels"][level]
            groups = {}
            for day in days if own["make"] else ():
                try:
                    key = bucket(level, date.fromisoformat(day.name))
                except ValueError:  # a folder that only looks like a date
                    continue
                if key < bucket(level, today):
                    groups.setdefault(key, []).append(day)
            for key, folders in groups.items():
                out = videos / cam / level / f"{cam}_{key}.mp4"
                if out.exists():
                    continue
                shots = [p for folder in folders for p in sorted(folder.glob("*.jpg")) if SHOT.fullmatch(p.name)
                         and own["from"] <= p.stem[-8:-3].replace("-", ":") <= own["to"]]
                try:
                    if shots:
                        build(shots, out, own["fps"], cfg["video"], f"{cam} {level} timelapse {key}")
                        print("built", out)
                except Exception as e:
                    problems.append(f"{cam}: {e}")
        got = len(list((images / cam / yesterday).glob("*.jpg")))
        if settled and got < len(cfg["times"]) and any(d.name < yesterday for d in days):  # skip a camera's first day
            problems.append(f"{cam}: only {got} of {len(cfg['times'])} snapshots were taken on {yesterday}")
    return problems


def time_limit(job, seconds):
    """End the whole job as a reported failure once it has run this long."""
    def stop(*_):
        print(f"PROBLEM: the {job} job ran too long and was stopped", flush=True)
        os._exit(1)  # at once: a stuck camera thread or ffmpeg would keep a normal exit waiting

    # A handler is needed: Python is process 1 in the container, and Linux drops signals that process does not handle.
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)


if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    # a hung camera or ffmpeg has to end as a failure, not as silence; a year of pictures can take hours to encode
    time_limit(job, 3600 if job == "grab" else 8 * 3600)
    if job not in ("grab", "stitch"):
        sys.exit("usage: timelapse.py grab|stitch")
    try:
        cfg = read_config()
        problems = [f"config: {e}" for e in validate(cfg)]
    except (OSError, ValueError) as e:  # unreadable or not JSON
        problems = [f"config: config.json could not be read: {e}"]
    if not problems:
        problems = grab(cfg) if job == "grab" else stitch(cfg, date.today())
    for p in problems:
        print("PROBLEM:", " ".join(p.split()))  # one line each, so the notifier can pick them out
    sys.exit(1 if problems else 0)
