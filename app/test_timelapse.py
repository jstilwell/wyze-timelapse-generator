"""Run inside the image: docker compose run --rm --entrypoint python stitch -m unittest discover -s /app -v"""
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import piexif

import timelapse as t
from rtsp_grabber import GrabError, grab_frame
import web

GOOD = {"cameras": [{"name": "cam1", "url": "rtsp://10.0.0.5:8554/live"}], "times": ["08:00", "12:00"],
        "stitch_time": "00:30", "fps": 24, "jpeg_quality": 95}


def jpeg(path, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=1,hue=h={seed}",
                    "-frames:v", "1", "-update", "1", str(path)], check=True)


class Buckets(unittest.TestCase):
    def test_week_straddling_new_year_stays_in_one_bucket_per_level(self):
        # Fri 2027-01-01 is in ISO week 2026-W53, whose Thursday is 2026-12-31
        d = date(2027, 1, 1)
        self.assertEqual([t.bucket(level, d) for level in t.LEVELS], ["2027-01-01", "2026-W53", "2026-12", "2026"])
        self.assertEqual(t.parent("daily", "2027-01-01"), "2026-W53")
        self.assertEqual(t.parent("weekly", "2026-W53"), "2026-12")
        self.assertEqual(t.parent("monthly", "2026-12"), "2026")

    def test_every_day_rolls_up_consistently(self):
        d = date(2024, 12, 1)
        while d < date(2028, 2, 1):
            week = t.parent("daily", d.isoformat())
            month = t.parent("weekly", week)
            self.assertEqual((week, month, t.parent("monthly", month)),
                             (t.bucket("weekly", d), t.bucket("monthly", d), t.bucket("yearly", d)))
            d += timedelta(days=1)


class Config(unittest.TestCase):
    def test_good(self):
        self.assertEqual(t.validate(GOOD), [])

    def test_rejects(self):
        for change in ({"cameras": []},
                       {"cameras": [{"name": "../etc", "url": "rtsp://x/live"}]},
                       {"cameras": [{"name": "cam1", "url": "http://x/live"}]},
                       {"cameras": GOOD["cameras"] * 2},
                       {"times": ["8:00"]}, {"times": ["25:00"]}, {"times": []},
                       {"stitch_time": "nope"}, {"fps": 0}, {"fps": "24"}, {"jpeg_quality": 101}):
            self.assertTrue(t.validate({**GOOD, **change}), change)

    def test_form_round_trip(self):
        form = {"cameras": ["cam1 rtsp://10.0.0.5:8554/live\n"], "times": ["08:00, 12:00"], "stitch_time": ["00:30"],
                "fps": ["24"], "jpeg_quality": ["95"]}
        self.assertEqual(web.from_form(form), GOOD)
        self.assertIn("&lt;script&gt;", web.render({**GOOD, "stitch_time": "<script>"}))


class Media(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.images, self.videos = Path(self.tmp.name, "images"), Path(self.tmp.name, "videos")
        self.addCleanup(self.tmp.cleanup)

    def test_probe_rejects_truncated_files(self):
        good = self.images / "good.jpg"
        jpeg(good)
        self.assertEqual(t.probe(good), {"nb_read_frames": 1, "width": 320, "height": 240})
        self.assertEqual(t.probe(good.read_bytes())["width"], 320)
        bad = self.images / "bad.jpg"
        bad.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
        with self.assertRaises(ValueError):
            t.probe(bad)

    def test_snapshot_writes_validated_tagged_jpeg_and_rejects_bad_frames(self):
        src = self.images / "src.jpg"
        jpeg(src)
        out = t.snapshot(lambda url, quality: src.read_bytes(), GOOD["cameras"][0], 95, self.images)
        self.assertEqual(out.parent.parent.name, "cam1")
        exif = piexif.load(str(out))
        self.assertEqual(exif["0th"][piexif.ImageIFD.Make], b"Wyze")
        self.assertRegex(exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal].decode(), r"^[+-]\d\d:\d\d$")
        self.assertEqual(exif["Exif"][piexif.ExifIFD.PixelXDimension], 320)
        self.assertEqual(t.probe(out)["nb_read_frames"], 1)

        t.time.sleep = lambda s: None
        for frame in (b"not a jpeg", src.read_bytes()[:3000]):
            with self.assertRaises(RuntimeError):
                t.snapshot(lambda url, quality: frame, GOOD["cameras"][0], 95, self.images)
        self.assertEqual(len(list(self.images.rglob("cam1_*"))), 1)  # nothing bad or half-written left behind

    def test_grab_error_never_leaks_the_camera_password(self):
        with self.assertRaises(GrabError) as caught:
            grab_frame("rtsp://admin:hunter2@127.0.0.1:1/live", timeout=3)
        self.assertNotIn("hunter2", str(caught.exception))

    def test_stitch_builds_every_level_and_reports_problems(self):
        # two snapshots a day from Mon 2025-12-01 to Tue 2026-01-06, "today" is Mon 2026-02-02
        day, n = date(2025, 12, 1), 0
        while day <= date(2026, 1, 6):
            for hour in ("08", "12"):
                jpeg(self.images / "cam1" / day.isoformat() / f"cam1_{day}_{hour}-00-00.jpg", n)
                n += 1
            day += timedelta(days=1)
        today = date(2026, 2, 2)
        self.assertEqual(t.stitch(GOOD, today, self.images, self.videos),
                         ["cam1: only 0 of 2 snapshots were taken on 2026-02-01"])

        def names(level):
            return sorted(p.name for p in (self.videos / "cam1" / level).iterdir())

        self.assertEqual(len(names("daily")), 37)
        self.assertEqual(names("weekly"), [f"cam1_2025-W{w}.mp4" for w in (49, 50, 51, 52)]
                         + ["cam1_2026-W01.mp4", "cam1_2026-W02.mp4"])
        self.assertEqual(names("monthly"), ["cam1_2025-12.mp4", "cam1_2026-01.mp4"])
        self.assertEqual(names("yearly"), ["cam1_2025.mp4"])  # 2026 has not ended
        # every frame survives each roll-up: Dec 1-28 is the four full weeks whose Thursday is in December
        self.assertEqual(t.probe(self.videos / "cam1" / "yearly" / "cam1_2025.mp4")["nb_read_frames"], 28 * 2)
        self.assertEqual(t.probe(self.videos / "cam1" / "monthly" / "cam1_2026-01.mp4")["nb_read_frames"], 9 * 2)

        # a second run is a no-op apart from the same warning
        before = {p: p.stat().st_mtime_ns for p in self.videos.rglob("*.mp4")}
        t.stitch(GOOD, today, self.images, self.videos)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.videos.rglob("*.mp4")})

    def test_corrupt_image_blocks_the_chain_and_is_reported(self):
        for d in ("2026-01-05", "2026-01-06"):
            jpeg(self.images / "cam1" / d / f"cam1_{d}_08-00-00.jpg")
        bad = self.images / "cam1" / "2026-01-06" / "cam1_2026-01-06_12-00-00.jpg"
        bad.write_bytes(next(self.images.rglob("*.jpg")).read_bytes()[:3000])
        problems = t.stitch({**GOOD, "times": ["08:00"]}, date(2026, 2, 2), self.images, self.videos)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("cam1_2026-01-06.mp4", problems[0])
        self.assertFalse((self.videos / "cam1" / "weekly").exists())
        self.assertEqual(list(self.videos.rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
