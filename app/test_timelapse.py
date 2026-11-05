"""Run inside the image: docker compose run --rm --entrypoint python stitch -m unittest discover -s /app -v"""
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

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
                       {"stitch_time": "nope"}, {"fps": 0}, {"fps": "24"}, {"fps": True}, {"jpeg_quality": 101}):
            self.assertTrue(t.validate({**GOOD, **change}), change)

    def test_hand_edited_nonsense_is_a_problem_not_a_crash(self):
        for cfg in ([], "x", {}, {**GOOD, "cameras": "cam1"}, {**GOOD, "cameras": [5]},
                    {**GOOD, "cameras": [{"name": 5, "url": None}]}, {**GOOD, "times": [8]}, {**GOOD, "times": None},
                    {**GOOD, "stitch_time": 30}):
            self.assertEqual(len(t.validate(cfg)), 1, cfg)


SECRET = "rtsp://admin:hunter2@10.0.0.5/live"
FORM = {"name": ["cam1", ""], "url": ["rtsp://10.0.0.5:8554/live", ""], "times": ["08:00, 12:00"],
        "stitch_time": ["00:30"], "fps": ["24"], "jpeg_quality": ["95"]}


class Form(unittest.TestCase):
    def test_round_trip_and_escaping(self):
        self.assertEqual(web.from_form(FORM), (GOOD, []))
        page = web.render({**GOOD, "stitch_time": "<script>", "cameras": [{"name": "<b>", "url": '"><i>'}]})
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>", page)
        self.assertNotIn("<i>", page)

    def test_saved_password_is_never_shown_and_survives_a_save(self):
        saved = [{"name": "cam1", "url": SECRET}]
        page = web.render({**GOOD, "cameras": saved})
        self.assertNotIn("hunter2", page)
        self.assertIn(web.mask(SECRET), page)
        # the masked address comes back unchanged: the stored password is kept
        cfg, errors = web.from_form({**FORM, "url": [web.mask(SECRET), ""]}, saved)
        self.assertEqual((cfg["cameras"], errors), (saved, []))
        # a different host with the mask must not receive the stored password
        cfg, errors = web.from_form({**FORM, "url": [web.mask(SECRET).replace("10.0.0.5", "evil.example"), ""]}, saved)
        self.assertNotIn("hunter2", json.dumps(cfg))
        self.assertEqual(len(errors), 1)
        # a password typed in a rejected submission is shown back, so it does not have to be typed twice
        typed = {**GOOD, "cameras": [{"name": "cam2", "url": "rtsp://a:new@10.0.0.6/live"}]}
        self.assertIn("a:new@", web.render(typed, saved=saved))

    def test_mask(self):
        for url, shown in ((SECRET, "rtsp://admin:••••@10.0.0.5/live"),
                           ("rtsp://u:p@ss@h:554/live", "rtsp://u:••••@h:554/live"),
                           ("rtsp://10.0.0.5:8554/live", "rtsp://10.0.0.5:8554/live"),
                           ("rtsp://user@host/live", "rtsp://user@host/live")):
            self.assertEqual(web.mask(url), shown)

    def test_evenly_spaced_times(self):
        cfg, errors = web.from_form({**FORM, "from": ["07:00"], "to": ["09:00"], "every": ["45"]})
        self.assertEqual((cfg["times"], errors), (["07:00", "07:45", "08:00", "08:30", "12:00"], []))
        for bad in ({"from": ["09:00"], "to": ["07:00"]}, {"from": ["07:00"]}, {"from": ["07:00"], "to": ["09:00"],
                    "every": ["1"]}, {"from": ["x"], "to": ["09:00"]}):
            self.assertEqual(len(web.from_form({**FORM, "every": ["30"], **bad})[1]), 1, bad)

    def test_video_lengths(self):
        self.assertIn("0.1 seconds", web.lengths({**GOOD, "times": ["08:00", "12:00", "16:00"]}))
        self.assertIn("a year's for about 15.2 minutes", web.lengths({**GOOD, "times": ["08:00"] * 60}))
        self.assertEqual(web.lengths({**GOOD, "fps": "x"}), "")


class Server(unittest.TestCase):
    """The real handler on a real socket, with the config and image folders pointed at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config, images = root / "config" / "config.json", root / "images"
        self.config.parent.mkdir()
        (self.config.parent / "config.example.json").write_text(json.dumps(GOOD))
        self.config.write_text(json.dumps({**GOOD, "cameras": [{"name": "cam1", "url": SECRET}]}))
        self.shot = images / "cam1" / "2026-11-05" / "cam1_2026-11-05_08-00-03.jpg"
        self.shot.parent.mkdir(parents=True)
        self.shot.write_bytes(b"jpeg bytes")
        (images / "secret.txt").write_text("not a snapshot")
        old = t.CONFIG, web.CONFIG, web.IMAGES
        t.CONFIG = web.CONFIG = self.config
        web.IMAGES = images
        self.addCleanup(lambda: (setattr(t, "CONFIG", old[0]), setattr(web, "CONFIG", old[1]),
                                 setattr(web, "IMAGES", old[2])))
        web.Handler.log_message = lambda *a: None
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = f"localhost:{self.server.server_port}"

    def ask(self, method, path, form=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(conn.close)
        headers = {"Host": self.host, "Origin": f"http://{self.host}", **headers}
        conn.request(method, path, urlencode(form, doseq=True) if form else None, headers)
        r = conn.getresponse()
        return r.status, r.read(), r

    def test_page_shows_the_latest_picture_and_no_password(self):
        status, body, r = self.ask("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"hunter2", body)
        self.assertIn(b"Latest picture: 2026-11-05 at 08:00", body)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.ask("GET", "/snapshot/cam1")[:2], (200, b"jpeg bytes"))

    def test_only_snapshots_are_served(self):
        for path in ("/snapshot/nope", "/snapshot/..", "/snapshot/../secret.txt", "/snapshot/%2e%2e/secret.txt",
                     "/snapshot/cam1/../../secret.txt", "/config/config.json", "/other"):
            self.assertEqual(self.ask("GET", path)[0], 404, path)

    def test_requests_from_other_sites_are_refused(self):
        self.assertEqual(self.ask("GET", "/", Host="evil.example")[0], 404)  # DNS rebinding
        self.assertEqual(self.ask("GET", "/snapshot/cam1", Host="evil.example")[0], 404)
        before = self.config.read_text()
        self.assertEqual(self.ask("POST", "/", FORM, Origin="http://evil.example")[0], 403)  # CSRF
        self.assertEqual(self.ask("POST", "/", FORM, Host="evil.example", Origin="http://evil.example")[0], 404)
        self.assertEqual(self.config.read_text(), before)

    def test_save_keeps_the_masked_password_and_redirects(self):
        status, _, r = self.ask("POST", "/", {**FORM, "url": [web.mask(SECRET), ""], "fps": ["12"]})
        self.assertEqual((status, r.getheader("Location")), (303, "/?saved"))
        saved = json.loads(self.config.read_text())
        self.assertEqual((saved["cameras"][0]["url"], saved["fps"]), (SECRET, 12))
        self.assertIn(b"Settings saved", self.ask("GET", "/?saved")[1])

    def test_rejected_save_changes_nothing_and_says_why(self):
        before = self.config.read_text()
        status, body, _ = self.ask("POST", "/", {**FORM, "times": ["25:00"]})
        self.assertEqual(status, 400)
        self.assertIn(b"Not saved", body)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.ask("POST", "/", FORM, **{"Content-Length": "x"})[0], 400)

    def test_unreadable_config_still_gives_a_usable_page(self):
        for junk in ("{not json", "[1, 2]", json.dumps({**GOOD, "cameras": [7]})):
            self.config.write_text(junk)
            status, body, _ = self.ask("GET", "/")
            self.assertEqual(status, 200, junk)
            self.assertIn(b"could not be read", body)

    def test_camera_test_reports_failure_without_saving_or_leaking(self):
        before = self.config.read_text()
        form = {**FORM, "name": ["cam1", "cam2"], "url": ["rtsp://admin:hunter2@127.0.0.1:1/live", "nonsense"],
                "action": ["test"]}
        status, body, _ = self.ask("POST", "/", form)
        self.assertEqual(status, 200)
        self.assertIn(b"Nothing answered at that address", body)
        self.assertIn(b"Fill in the whole address first", body)
        self.assertNotIn(b"hunter2", body.replace(b'value="rtsp://admin:hunter2@', b""))  # only where it was typed
        self.assertEqual(self.config.read_text(), before)

    def test_camera_test_shows_the_live_picture(self):
        old = web.grab_frame
        self.addCleanup(lambda: setattr(web, "grab_frame", old))
        web.grab_frame = lambda url, quality, timeout: b"frame" if url == SECRET else self.fail(url)
        # the saved address comes back masked, and the test still reaches the camera with the stored password
        body = self.ask("POST", "/", {**FORM, "url": [web.mask(SECRET), ""], "action": ["test"]})[1]
        self.assertIn(b'src="data:image/jpeg;base64,ZnJhbWU="', body)
        self.assertNotIn(b"hunter2", body)


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
        self.assertIn(f"{bad.name} is corrupt", problems[0])  # names the file to delete
        self.assertFalse((self.videos / "cam1" / "weekly").exists())
        self.assertEqual(list(self.videos.rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
