"""Wyze timelapse jobs.

grab   - one validated, EXIF-tagged snapshot per camera
stitch - turn finished days into daily videos, then roll them up into weekly, monthly and yearly ones
"""
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import piexif

from rtsp_grabber import GrabError, grab_frame

CONFIG = Path("/config/config.json")
IMAGES, VIDEOS = Path("/images"), Path("/videos")
LEVELS = ["daily", "weekly", "monthly", "yearly"]
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def read_config():
    path = CONFIG if CONFIG.exists() else CONFIG.with_name("config.example.json")
    return json.loads(path.read_text())


def validate(cfg):
    """Problems with a config dict (empty list = valid). Camera names become directory names, so they are strict."""
    errs = []
    cams = cfg.get("cameras") or []
    names = [c.get("name", "") for c in cams]
    if not cams:
        errs.append("at least one camera is required")
    if len(set(names)) != len(names):
        errs.append("camera names must be unique")
    for c in cams:
        if not NAME.fullmatch(c.get("name", "")):
            errs.append(f"camera name {c.get('name')!r}: use a-z, 0-9, - and _ (max 32 characters)")
        if not re.fullmatch(r"rtsps?://\S+", c.get("url", "")):
            errs.append(f"camera {c.get('name')!r}: URL must look like rtsp://address:port/path")
    times = cfg.get("times") or []
    if not times or len(set(times)) != len(times) or not all(HHMM.fullmatch(t) for t in times):
        errs.append("snapshot times: one or more different 24-hour HH:MM values")
    if not HHMM.fullmatch(cfg.get("stitch_time", "")):
        errs.append("stitch time: a 24-hour HH:MM value")
    for key, lo, hi in (("fps", 1, 60), ("jpeg_quality", 1, 100)):
        if not (isinstance(cfg.get(key), int) and lo <= cfg[key] <= hi):
            errs.append(f"{key}: a whole number from {lo} to {hi}")
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

def snapshot(grab, cam, quality, images):
    for attempt in range(3):
        try:
            jpeg = grab(cam["url"], quality)
            size = probe(jpeg)
            break
        except (GrabError, ValueError) as e:
            error = e
            time.sleep(2)
    else:
        raise RuntimeError(f"no usable frame after 3 attempts: {error}")

    now = datetime.now().astimezone()
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

    day = images / cam["name"] / f"{now:%Y-%m-%d}"
    day.mkdir(parents=True, exist_ok=True)
    final = day / f"{cam['name']}_{now:%Y-%m-%d_%H-%M-%S}.jpg"
    part = final.with_suffix(".part")
    try:
        part.write_bytes(tagged.getvalue())
        # Re-check the file as it actually landed on the mount before it gets a name the stitcher picks up.
        probe(part)
        if piexif.load(str(part))["Exif"].get(piexif.ExifIFD.DateTimeOriginal) != stamp.encode():
            raise RuntimeError("snapshot metadata did not survive the write")
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
    return final


def grab(cfg, images=IMAGES):
    problems = []
    for cam in cfg["cameras"]:
        try:
            print("saved", snapshot(grab_frame, cam, cfg["jpeg_quality"], images))
        except Exception as e:
            problems.append(f"{cam['name']}: {e}")
    return problems


# ---- stitch --------------------------------------------------------------------------------------------------------

def bucket(level, d):
    """Name of the video that date `d` belongs to at `level`.

    Weeks are ISO weeks, and a week belongs to the month and year of its Thursday, so every day ends up
    in exactly one weekly, one monthly and one yearly video even though weeks straddle month ends.
    """
    y, w, _ = d.isocalendar()
    thursday = date.fromisocalendar(y, w, 4)
    return {"daily": d.isoformat(), "weekly": f"{y}-W{w:02}", "monthly": f"{thursday:%Y-%m}", "yearly": str(y)}[level]


def parent(level, key):
    """The bucket one level up that the bucket `key` rolls into."""
    if level == "daily":
        return bucket("weekly", date.fromisoformat(key))
    if level == "weekly":
        return bucket("monthly", date.fromisocalendar(int(key[:4]), int(key[6:]), 4))
    return key[:4]


def build(parts, out, fps, title):
    """Make `out` from images (encode) or videos (lossless join), fully decode the result, then move it into place."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part")
    meta = ["-metadata", f"title={title}",
            "-metadata", f"creation_time={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
            "-movflags", "+faststart", "-f", "mp4", str(part)]
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
        if parts[0].suffix == ".jpg":
            expected = len(parts)
            source = ["-framerate", str(fps), "-pattern_type", "glob", "-i", str(parts[0].parent / "*.jpg"),
                      # no B-frames: keeps timestamps simple so the joins higher up are seamless
                      "-c:v", "libx264", "-crf", "18", "-bf", "0", "-pix_fmt", "yuv420p",
                      "-metadata", f"comment={expected} snapshots at {fps} fps"]
        else:
            # shortcut: stream copy needs every part to share resolution and codec settings; if a camera's
            # resolution changes the join fails validation below, switch this to a re-encode then.
            expected = sum(probe(p)["nb_read_frames"] for p in parts)
            listing.write("".join(f"file '{p}'\n" for p in parts))
            listing.flush()
            source = ["-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy",
                      "-metadata", f"comment={expected} frames joined from {len(parts)} videos"]
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *source, *meta], capture_output=True, text=True)
    try:
        if r.returncode or r.stderr.strip():
            raise RuntimeError(f"ffmpeg failed building {out.name}: {r.stderr.strip()[-300:]}")
        frames = probe(part)["nb_read_frames"]
        if frames != expected:
            raise RuntimeError(f"{out.name} has {frames} frames, expected {expected}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)


def stitch(cfg, today, images=IMAGES, videos=VIDEOS):
    """Build every missing video whose period has ended. Safe to re-run: it catches up on anything a missed run left."""
    problems = []
    for cam in (c["name"] for c in cfg["cameras"]):
        days = sorted(d for d in (images / cam).glob("????-??-??") if d.name < today.isoformat())
        try:
            groups = {d.name: sorted(d.glob("*.jpg")) for d in days}
            for i, level in enumerate(LEVELS):
                for key, parts in groups.items():
                    out = videos / cam / level / f"{cam}_{key}.mp4"
                    if parts and not out.exists():
                        build(parts, out, cfg["fps"], f"{cam} {level} timelapse {key}")
                        print("built", out)
                if level == "yearly":
                    break
                # Next level up: this level's videos grouped by parent bucket, for parents that have ended.
                groups = {}
                for video in sorted((videos / cam / level).glob(f"{cam}_*.mp4")):
                    up = parent(level, video.stem[len(cam) + 1:])
                    if up < bucket(LEVELS[i + 1], today):
                        groups.setdefault(up, []).append(video)
        except Exception as e:
            # Stopping here keeps a longer video from being built over a hole; the next run retries.
            problems.append(f"{cam}: {e}")
        yesterday = (today - timedelta(days=1)).isoformat()
        got = len(list((images / cam / yesterday).glob("*.jpg")))
        if got < len(cfg["times"]) and any(d.name < yesterday for d in days):  # skip a camera's first day
            problems.append(f"{cam}: only {got} of {len(cfg['times'])} snapshots were taken on {yesterday}")
    return problems


if __name__ == "__main__":
    signal.alarm(3600)  # a hung camera or ffmpeg has to end as a failure, not as silence
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job not in ("grab", "stitch"):
        sys.exit("usage: timelapse.py grab|stitch")
    cfg = read_config()
    problems = [f"config: {e}" for e in validate(cfg)]
    if not problems:
        problems = grab(cfg) if job == "grab" else stitch(cfg, date.today())
    for p in problems:
        print("PROBLEM:", " ".join(p.split()))  # one line each, so the notifier can pick them out
    sys.exit(1 if problems else 0)
