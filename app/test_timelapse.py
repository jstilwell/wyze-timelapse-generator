"""Run inside the image: docker compose run --rm --entrypoint python stitch -m unittest discover -s /app -v"""
import base64
import http.client
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import piexif

import timelapse as t
import view
from rtsp_grabber import GrabError, grab_frame
import web

# written the old way, with one speed for everything: with_defaults turns it into today's layout
GOOD = t.with_defaults({"cameras": [{"name": "cam1", "url": "rtsp://10.0.0.5:8554/live"}], "times": ["08:00", "12:00"],
                        "stitch_time": "00:30", "fps": 24, "jpeg_quality": 95})


def changed(video=None, **levels):
    """GOOD with some look settings and some per-kind-of-video settings replaced."""
    return {**GOOD, "video": {**GOOD["video"], **(video or {})},
            "levels": {level: {**GOOD["levels"][level], **levels.get(level, {})} for level in t.LEVELS}}


def stream(path, entry):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", f"stream={entry}",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return r.stdout.strip()


# Throwaway self-signed certificates for CN/SAN camera.test, made for these tests only and used nowhere else:
#   openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 36500 -subj /CN=camera.test \
#     -addext subjectAltName=DNS:camera.test
CAMERA_CERT = """-----BEGIN CERTIFICATE-----
MIIBeTCCASCgAwIBAgIUXDdpMG1qw5BHyIVFvbJRvWZAff0wCgYIKoZIzj0EAwIw
FjEUMBIGA1UEAwwLY2FtZXJhLnRlc3QwIBcNMjYxMTA1MTYxMjQ3WhgPMjEyNjEw
MTIxNjEyNDdaMBYxFDASBgNVBAMMC2NhbWVyYS50ZXN0MFkwEwYHKoZIzj0CAQYI
KoZIzj0DAQcDQgAE3LLSVbfL3EnHfAJtS1Aj6v/241cn1l53Pm4jXxvRKQWl8m73
93UULnEc2guXqZ5DG/xiFbJL/2o19wWshsQWuaNKMEgwHQYDVR0OBBYEFAd3MP69
0hVtVs96h47vWqHNTEZ/MA8GA1UdEwEB/wQFMAMBAf8wFgYDVR0RBA8wDYILY2Ft
ZXJhLnRlc3QwCgYIKoZIzj0EAwIDRwAwRAIgKjplZ5caRwlnQcbBEpi037jK62lO
Wp5Ne+zWM/yMlNQCIDXq8YTB8TXETuY7e04lM0duZWU44iLkoG1R5vk5CEOL
-----END CERTIFICATE-----
"""
CAMERA_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgdtfIz0JafOI6BrVZ
3dkS1M7DqwYyBsbljsP6UEM3vxihRANCAATcstJVt8vcScd8Am1LUCPq//bjVyfW
Xnc+biNfG9EpBaXybvf3dRQucRzaC5epnkMb/GIVskv/ajX3BayGxBa5
-----END PRIVATE KEY-----
"""
OTHER_CERT = """-----BEGIN CERTIFICATE-----
MIIBejCCASCgAwIBAgIUEpShr9svyFnGDxnxA83BVJgUK1swCgYIKoZIzj0EAwIw
FjEUMBIGA1UEAwwLY2FtZXJhLnRlc3QwIBcNMjYxMTA1MTYxMjQ3WhgPMjEyNjEw
MTIxNjEyNDdaMBYxFDASBgNVBAMMC2NhbWVyYS50ZXN0MFkwEwYHKoZIzj0CAQYI
KoZIzj0DAQcDQgAEvpNLd7v/ce5pslFZbs/l9Tbxlk8xpVDm3UbY3rDGilgx5qj7
Awq2JOnTsb3CG8vYtxzUYzzoqaa6gJfyDl0a0aNKMEgwHQYDVR0OBBYEFJGASGx2
zjxVooPqdb0lKaCOu3afMA8GA1UdEwEB/wQFMAMBAf8wFgYDVR0RBA8wDYILY2Ft
ZXJhLnRlc3QwCgYIKoZIzj0EAwIDSAAwRQIhAMFNf8DGNVLaLuYqGFoJnnbdsDNa
cDbLqKPCdKgo1rArAiBPMYFkzmJAJH5xBblpMFiiUAwfBc/8PZSRA9j4QERgfw==
-----END CERTIFICATE-----
"""


def fake_rtsps_camera(test):
    """A local TLS listener presenting CAMERA_CERT. Returns its rtsps address and a list that grows by one for
    every handshake a client completed, which a client only does once it has accepted the certificate."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    Path(tmp.name, "cert.pem").write_text(CAMERA_CERT)
    Path(tmp.name, "key.pem").write_text(CAMERA_KEY)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(Path(tmp.name, "cert.pem"), Path(tmp.name, "key.pem"))
    listener = socket.create_server(("127.0.0.1", 0))
    test.addCleanup(listener.close)
    handshakes = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with tls.wrap_socket(conn, server_side=True) as client:
                    client.settimeout(2)
                    client.recv(1)  # a client that rejects the certificate says so here instead of sending data
                    handshakes.append(1)
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    return f"rtsps://127.0.0.1:{listener.getsockname()[1]}/live", handshakes


def jpeg(path, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=1,hue=h={seed}",
                    "-frames:v", "1", "-update", "1", str(path)], check=True)


class Buckets(unittest.TestCase):
    def test_weeks_are_iso_weeks_and_months_and_years_are_the_calendars(self):
        # Fri 2027-01-01 is in ISO week 2026-W53
        self.assertEqual([t.bucket(level, date(2027, 1, 1)) for level in t.LEVELS],
                         ["2027-01-01", "2026-W53", "2027-01", "2027"])


class Config(unittest.TestCase):
    def test_good(self):
        self.assertEqual(t.validate(GOOD), [])

    def test_rejects(self):
        for change in ({"cameras": []},
                       {"cameras": [{"name": "../etc", "url": "rtsp://x/live"}]},
                       {"cameras": [{"name": "cam1", "url": "http://x/live"}]},
                       {"cameras": GOOD["cameras"] * 2},
                       {"times": ["8:00"]}, {"times": ["25:00"]}, {"times": []},
                       {"stitch_time": "nope"}, {"jpeg_quality": 101}):
            self.assertTrue(t.validate({**GOOD, **change}), change)
        for cfg in (changed(daily={"fps": 0}), changed(daily={"fps": "24"}), changed(daily={"fps": True}),
                    changed(yearly={"make": "yes"}), changed(weekly={"from": "13:00", "to": "11:00"}),
                    changed(weekly={"to": "noon"}), changed({"size": "8000"}), changed({"size": ["1080"]}),
                    changed({"stamp": "date':x"}), changed({"mirror": "on"}), changed({"contrast": 500}),
                    changed({"brightness": 1.5})):
            self.assertEqual(len(t.validate(cfg)), 1, cfg)

    def test_a_password_the_page_could_not_hide_is_refused(self):
        def problems(url):
            return t.validate({**GOOD, "cameras": [{"name": "cam1", "url": url}]})

        self.assertEqual(problems("rtsp://admin:pa/ss@10.0.0.5/live"), ["camera 'cam1': write a / in the password as %2F"])
        self.assertEqual(web.mask("rtsp://admin:pa%2Fss@10.0.0.5/live"), "rtsp://admin:••••@10.0.0.5/live")
        for fine in ("rtsp://admin:pa%2Fss@10.0.0.5/live", "rtsp://admin:p@ss@10.0.0.5/live", "rtsp://10.0.0.5/live"):
            self.assertEqual(problems(fine), [], fine)

    def test_older_config_files_keep_working(self):
        old = {"cameras": GOOD["cameras"], "times": ["08:00"], "stitch_time": "00:30", "fps": 12, "jpeg_quality": 90}
        cfg = t.with_defaults(dict(old))
        self.assertEqual(t.validate(cfg), [])
        self.assertEqual({own["fps"] for own in cfg["levels"].values()}, {12})
        self.assertEqual(cfg["video"], t.VIDEO)
        part = t.with_defaults({**old, "levels": {"yearly": {"from": "11:00"}}, "video": {"stamp": "date"}})
        self.assertEqual((part["levels"]["yearly"], part["levels"]["daily"]["from"], part["video"]["stamp"]),
                         ({"make": True, "fps": 12, "from": "11:00", "to": "23:59"}, "00:00", "date"))

    def test_hand_edited_nonsense_is_a_problem_not_a_crash(self):
        for cfg in ([], "x", {}, {**GOOD, "cameras": "cam1"}, {**GOOD, "cameras": [5]},
                    {**GOOD, "cameras": [{"name": 5, "url": None}]}, {**GOOD, "times": [8]}, {**GOOD, "times": None},
                    {**GOOD, "stitch_time": 30}, {**GOOD, "video": []}, {**GOOD, "levels": {"daily": 5}},
                    changed(daily={"from": 8}), t.with_defaults({"video": 3})):
            self.assertEqual(len(t.validate(cfg)), 1, cfg)


SECRET = "rtsp://admin:hunter2@10.0.0.5/live"
FORM = {"name": ["cam1", ""], "url": ["rtsp://10.0.0.5:8554/live", ""], "times": ["08:00, 12:00"],
        "stitch_time": ["00:30"], "jpeg_quality": ["95"],
        **{f"{level}_{key}": [value] for level in t.LEVELS
           for key, value in (("make", "on"), ("fps", "24"), ("from", "00:00"), ("to", "23:59"))},
        **{key: [str(value)] for key, value in t.VIDEO.items() if value is not False},  # switches are left unticked
        "view_model": ["claude-opus-5-5"], "view_compare": ["on"]}


class Alerts(unittest.TestCase):
    FILLED = {"ntfy_url": "https://ntfy.sh/secret-topic", "telegram_token": "123456:ABC-def_1",
              "telegram_chat": "-100123", "pushover_token": "a" * 30, "pushover_user": "b" * 30,
              "email_provider": "cloudflare", "email_key": "cf-key_1", "email_account": "0" * 32,
              "email_from": "alerts@example.com", "email_to": "me@example.com"}

    def test_an_older_config_has_every_channel_off(self):
        self.assertEqual(GOOD["alerts"], dict.fromkeys(t.ALERT_FIELDS, ""))
        self.assertEqual(t.alert_problems(GOOD["alerts"]), [])
        self.assertFalse(t.well_formed({**GOOD, "alerts": {**GOOD["alerts"], "email_to": 5}}))

    def test_a_filled_in_form_is_accepted(self):
        cfg, errors = web.from_form({**FORM, **{key: [value] for key, value in self.FILLED.items()}})
        self.assertEqual((cfg["alerts"], errors), (self.FILLED, []))
        self.assertEqual(t.alert_problems(cfg["alerts"]), [])

    def test_a_half_filled_channel_is_refused(self):
        for given, word in (({"telegram_token": "1:a"}, "Telegram"), ({"telegram_chat": "1"}, "Telegram"),
                            ({"pushover_user": "b"}, "Pushover"), ({"email_provider": "resend"}, "Email"),
                            ({**self.FILLED, "email_account": ""}, "Cloudflare")):
            problems = t.alert_problems({**t.ALERTS, **given})
            self.assertEqual(len(problems), 1, given)
            self.assertIn(word, problems[0])

    def test_email_is_off_with_no_provider_even_with_its_boxes_filled(self):
        self.assertEqual(t.alert_problems({**t.ALERTS, "email_key": "k", "email_to": "me@example.com"}), [])

    def test_a_value_that_would_break_out_of_the_curl_config_is_refused(self):
        for key, value in (("ntfy_url", 'https://a"\nurl = "file:///etc/passwd'), ("ntfy_url", "file:///etc/passwd"),
                           ("ntfy_url", "https://a\\b"), ("telegram_token", '1:a"b'), ("pushover_token", 'a"'),
                           ("pushover_user", "a\nb"), ("email_key", 'k"'), ("email_account", "zz"),
                           ("email_to", "no-at-sign"), ("email_provider", "smtp")):
            problems = t.alert_problems({**self.FILLED, key: value})
            self.assertTrue(problems, (key, value))
            self.assertNotIn(value, " ".join(problems))  # a mistyped key is not repeated back

    def test_a_masked_key_stays_and_an_emptied_one_goes(self):
        form = {**FORM, **{key: [web.MASK if key in web.ALERT_SECRETS else value]
                           for key, value in self.FILLED.items()}}
        self.assertEqual(web.from_form(form, alerts=self.FILLED), ({**GOOD, "alerts": self.FILLED}, []))
        self.assertEqual(web.from_form({**form, "telegram_token": [""]}, alerts=self.FILLED)[0]["alerts"]
                         ["telegram_token"], "")

    def test_a_gmail_app_password_may_be_pasted_with_its_spaces(self):
        form = {**FORM, "email_provider": ["gmail"], "email_key": ["abcd efgh ijkl mnop"],
                "email_from": ["me@gmail.com"], "email_to": ["me@gmail.com"]}
        cfg, errors = web.from_form(form)
        self.assertEqual((cfg["alerts"]["email_key"], errors), ("abcdefghijklmnop", []))
        self.assertEqual(t.alert_problems(cfg["alerts"]), [])

    def test_a_saved_key_is_not_sent_to_a_different_provider(self):
        form = {**FORM, "email_provider": ["resend"], "email_key": [web.MASK]}
        self.assertEqual(len(web.from_form(form, alerts=self.FILLED)[1]), 1)
        self.assertEqual(web.from_form({**form, "email_key": ["new"]}, alerts=self.FILLED)[1], [])


class Form(unittest.TestCase):
    def test_round_trip_and_escaping(self):
        self.assertEqual(web.from_form(FORM), (GOOD, []))
        page = web.render({**GOOD, "stitch_time": "<script>", "cameras": [{"name": "<b>", "url": '"><i>'}]})
        self.assertIn('value="&lt;script&gt;"', page)
        self.assertEqual(page.count("<script>"), 1)  # the page's own; the injected one stayed text
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

    def test_stored_certificate_stays_with_its_host_and_port_only(self):
        saved = [{"name": "cam1", "url": "rtsps://10.0.0.5:7441/abc", "cert": "PEM", "cert_host": "camera.test"}]

        def camera(url, **extra):
            return web.from_form({**FORM, "url": [url, ""], **extra}, saved)[0]["cameras"][0]

        self.assertEqual(camera("rtsps://10.0.0.5:7441/new-path"), {**saved[0], "url": "rtsps://10.0.0.5:7441/new-path"})
        for url, extra in (("rtsps://10.0.0.6:7441/abc", {}), ("rtsps://10.0.0.5:7442/abc", {}),
                           ("rtsp://10.0.0.5:7441/abc", {}), ("rtsps://10.0.0.5:7441/abc", {"repin": ["0"]})):
            self.assertNotIn("cert", camera(url, **extra), (url, extra))

    def test_view_check_settings_come_back_from_the_form(self):
        cfg, errors = web.from_form({**FORM, "check_view": ["on"], "view_notes": ["  The <post> is always there. "]})
        self.assertEqual((cfg["check_view"], cfg["view_notes"], errors + t.validate(cfg)),
                         (True, "The <post> is always there.", []))
        page = web.render(cfg)
        self.assertIn('name="check_view" checked', page)
        self.assertIn("The &lt;post&gt; is always there.</textarea>", page)
        self.assertNotIn('name="check_view" checked', web.render(GOOD))
        self.assertEqual(len(t.validate({**GOOD, "view_notes": "x" * 2001})), 1)
        self.assertEqual(len(t.validate({**GOOD, "check_view": "yes"})), 1)

    def test_model_choice_and_cost_estimates_follow_the_settings(self):
        form = {**FORM, "view_model": ["claude-haiku-5-5"]}
        del form["view_compare"]
        cfg, errors = web.from_form(form)
        self.assertEqual((cfg["view_model"], cfg["view_compare"], errors + t.validate(cfg)), ("claude-haiku-5-5", False, []))
        self.assertEqual(len(t.validate({**GOOD, "view_model": "gpt-9"})), 1)
        self.assertEqual(len(t.validate({**GOOD, "view_compare": "on"})), 1)
        # GOOD: 1 camera, 2 pictures a day = 60 checks a month; 450 + 760 per picture tokens in, 250 out
        self.assertAlmostEqual(web.monthly(GOOD, "claude-opus-5-5"), 60 * (1970 * 4 + 250 * 20) / 1e6)
        self.assertAlmostEqual(web.monthly(GOOD, "claude-opus-5-5", compare=False), 60 * (1210 * 4 + 250 * 20) / 1e6)
        self.assertAlmostEqual(web.monthly({**GOOD, "view_compare": False}, "claude-haiku-5-5"),
                               60 * (1210 * 0.10 + 250 * 0.50) / 1e6)
        busy = {**GOOD, "cameras": GOOD["cameras"] * 3, "times": ["08:00"] * 48, "view_notes": "x" * 400}
        self.assertAlmostEqual(web.monthly(busy, "claude-sonnet-5-5"), 3 * 48 * 30 * (2070 * 2 + 250 * 10) / 1e6)
        self.assertEqual([web.money(d) for d in (0.001, 0.034, 0.77, 1.2, 19.99, 56.4)],
                         ["under 1¢", "3¢", "77¢", "$1.20", "$19.99", "$56"])
        page = web.render(GOOD)
        self.assertIn('<option value="claude-opus-5-5" selected>Claude Opus 5.5: about 77¢ a month</option>', page)
        self.assertIn("On Claude Opus 5.5 that is about 77¢ a month with the previous picture and 59¢ without.", page)
        self.assertIn("Estimated for 1 camera and 2 pictures a day", page)
        for model, (name, dollars_in, _) in t.VIEW_MODELS.items():  # every model is in the comparison, with its price
            self.assertIn(f'<th scope="row">{name}</th>', page)
            self.assertIn(f"${dollars_in:.2f} in", page)
        self.assertEqual(set(web.EXPECT), set(t.VIEW_MODELS))

    def test_look_and_per_video_settings_come_back_from_the_form(self):
        form = {**FORM, "yearly_from": ["11:00"], "yearly_to": ["13:00"], "yearly_fps": ["30"], "size": ["1080"],
                "stamp": ["datetime"], "deflicker": ["on"], "brightness": ["-10"]}
        del form["daily_make"]
        cfg, errors = web.from_form(form)
        self.assertEqual((errors, t.validate(cfg)), ([], []))
        self.assertEqual(cfg, changed({"size": "1080", "stamp": "datetime", "deflicker": True, "brightness": -10},
                                      daily={"make": False}, yearly={"from": "11:00", "to": "13:00", "fps": 30}))
        page = web.render(cfg)
        for shown in ('name="deflicker" checked', 'value="1080" selected', 'id="yearly_from" name="yearly_from" '
                      'type="time" value="11:00"', 'id="brightness" name="brightness" type="number" min="-50" '
                      'max="50" value="-10"'):
            self.assertIn(shown, page)
        self.assertNotIn('name="daily_make" checked', page)
        # anything that is not one of the offered choices is refused, so nothing typed reaches ffmpeg
        cfg, _ = web.from_form({**FORM, "shape": ["16:9,drawtext=textfile=/config/config.json"]})
        self.assertEqual(len(t.validate(cfg)), 1)

    def test_video_lengths(self):
        self.assertIn("2 pictures a day: plays for about 0.1 seconds", web.plays(GOOD, "daily"))
        self.assertIn("30 seconds", web.plays(GOOD, "yearly"))
        self.assertIn("1 picture a day: plays for about 73 seconds",
                      web.plays(changed(yearly={"from": "11:00", "to": "13:00", "fps": 5}), "yearly"))
        self.assertIn("No picture time", web.plays(changed(weekly={"from": "20:00"}), "weekly"))
        self.assertEqual(web.plays(changed(daily={"make": False}), "daily"), "Not made.")


def fake_claude(test, *answers):
    """A stand-in for api.anthropic.com, reached through the SDK's own ANTHROPIC_BASE_URL setting. Each request
    gets the next answer: a (blocked, reason) verdict, "refusal", or an HTTP error status. Returns the requests."""
    script, requests = list(answers), []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body})
            answer = script.pop(0)
            if isinstance(answer, int):
                status, reply = answer, {"type": "error", "error": {"type": "authentication_error", "message": "no"}}
            else:
                refused = answer == "refusal"
                text = "" if refused else json.dumps({"blocked": answer[0], "reason": answer[1]})
                status, reply = 200, {
                    "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [] if refused else [{"type": "text", "text": text}],
                    "stop_reason": "refusal" if refused else "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 2000, "output_tokens": 30}}
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    old = {key: os.environ.get(key) for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")}
    test.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                             for k, v in old.items()])
    os.environ.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", ANTHROPIC_API_KEY="sk-test-key")
    return requests


class ViewCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.images = Path(self.tmp.name, "images")
        self.shot = self.images / "cam1" / "2026-11-04" / "cam1_2026-11-04_12-00-00.jpg"
        jpeg(self.shot)
        self.frame = self.shot.read_bytes()
        old = t.time.sleep
        self.addCleanup(lambda: setattr(t.time, "sleep", old))
        self.slept = []
        t.time.sleep = self.slept.append

    def test_request_carries_both_pictures_the_notes_and_asks_for_a_strict_verdict(self):
        requests = fake_claude(self, (True, "a  cat sitting\nright in front of the lens"), (False, ""))
        self.assertEqual(view.blocked(self.frame, "cam1", self.shot, "The post is always there."),
                         "a cat sitting right in front of the lens")
        self.assertEqual(view.blocked(self.frame, "cam1", model="claude-haiku-5-5"), "")
        first, second = (r["body"] for r in requests)
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(second["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", second)  # Haiku has no fallback and refuses the parameter
        self.assertNotIn("anthropic-beta", requests[1]["headers"])
        self.assertIn("server-side-fallback-2026-07-01", requests[0]["headers"]["anthropic-beta"])
        self.assertIn("sk-test-key", requests[0]["headers"]["authorization"])  # the SDK took the key from .env
        self.assertEqual(first["output_config"], {"effort": "low", "format": {"type": "json_schema",
                                                                              "schema": view.VERDICT}})
        self.assertNotIn("temperature", first)  # refused by current models
        kinds = [block["type"] for block in first["messages"][0]["content"]]
        self.assertEqual(kinds, ["text", "image", "text", "image", "text", "text"])
        self.assertIn("<notes>\nThe post is always there.\n</notes>", first["messages"][0]["content"][4]["text"])
        sent = base64.b64decode(first["messages"][0]["content"][3]["source"]["data"])
        self.assertEqual(t.probe(sent)["width"], 320)  # small pictures are not enlarged
        # no earlier picture and no notes: just the new picture and the question
        self.assertEqual([block["type"] for block in second["messages"][0]["content"]], ["text", "image", "text"])

    def test_failures_say_what_went_wrong_without_the_key(self):
        requests = fake_claude(self, 401, "refusal")
        for expected in ("did not accept ANTHROPIC_API_KEY", "no verdict (refusal)"):
            with self.assertRaises(view.CheckError) as caught:
                view.blocked(self.frame, "cam1")
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("sk-test-key", str(caught.exception))
        os.environ["ANTHROPIC_API_KEY"] = ""
        with self.assertRaises(view.CheckError) as caught:
            view.blocked(self.frame, "cam1")
        self.assertIn("ANTHROPIC_API_KEY is not set", str(caught.exception))
        self.assertEqual(len(requests), 2)  # nothing was sent without a key

    def snap(self, verdicts, frames=None):
        """Run snapshot() with a scripted check; returns (grabs made, note, which frame was saved)."""
        made, grabs, self.compared = [], [], []
        for seed in range(5):
            jpeg(Path(self.tmp.name, f"frame{seed}.jpg"), seed * 60)
            made.append(Path(self.tmp.name, f"frame{seed}.jpg").read_bytes())

        def grab(url, quality, **pin):
            grabs.append(1)
            if frames and len(grabs) > frames:
                raise GrabError("camera went away")
            return made[len(grabs) - 1]

        def check(frame, camera, usual):
            self.compared.append((camera, usual))
            verdict = verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        out, note = t.snapshot(grab, GOOD["cameras"][0], 95, self.images, check)
        kept = next(i for i, m in enumerate(made) if m[-2000:] == out.read_bytes()[-2000:])
        return len(grabs), note, kept

    def test_blocked_pictures_are_retaken_until_the_view_clears(self):
        self.assertEqual(self.snap(["a person", "a person", ""]), (3, None, 2))
        self.assertEqual(self.slept, [t.RETAKE_WAIT] * 2)
        self.assertEqual(self.compared, [("cam1", self.shot)] * 3)  # the last saved picture is the comparison

    def test_a_picture_is_always_kept_and_the_alert_says_why(self):
        grabs, note, kept = self.snap(["a tarp"] * 4)
        self.assertEqual((grabs, kept), (4, 3))
        self.assertIn("still blocked after 4 pictures (a tarp)", note)
        grabs, note, kept = self.snap([view.CheckError("Anthropic could not be reached")])
        self.assertEqual((grabs, kept), (1, 0))
        self.assertIn("could not be checked (Anthropic could not be reached); the picture was kept", note)
        grabs, note, kept = self.snap(["a bird"], frames=1)
        self.assertEqual(kept, 0)
        self.assertIn("blocked (a bird) and the retake failed", note)

    def test_a_picture_kept_with_its_view_blocked_is_not_the_usual_view_next_time(self):
        self.snap(["a tarp"] * 4)
        self.snap([""])
        self.assertEqual(self.compared, [("cam1", self.shot)])
        self.assertNotEqual(t.latest("cam1", self.images), self.shot)  # the settings page still shows the newest

    def test_grab_job_checks_only_when_switched_on_and_reports_a_view_that_stays_blocked(self):
        old = t.grab_frame
        self.addCleanup(lambda: setattr(t, "grab_frame", old))
        t.grab_frame = lambda url, quality, **pin: self.frame
        requests = fake_claude(self, *[(True, "a ladder")] * 4)
        self.assertEqual(t.grab(GOOD, self.images), [])
        self.assertEqual(requests, [])
        problems = t.grab({**GOOD, "check_view": True, "view_notes": "Ignore the hose."}, self.images)
        self.assertEqual(len(problems), 1)
        self.assertIn("cam1: the view was still blocked after 4 pictures (a ladder)", problems[0])
        self.assertEqual(len(requests), 4)
        self.assertIn("Ignore the hose.", json.dumps(requests[0]["body"]))

        def pictures(request):
            return sum(block["type"] == "image" for block in request["body"]["messages"][0]["content"])

        self.assertEqual((pictures(requests[0]), requests[0]["body"]["model"]), (2, "claude-opus-5-5"))
        fake = fake_claude(self, (False, ""))
        t.grab({**GOOD, "check_view": True, "view_compare": False, "view_model": "claude-sonnet-5-5"}, self.images)
        self.assertEqual((pictures(fake[0]), fake[0]["body"]["model"]), (1, "claude-sonnet-5-5"))
        self.assertGreater(len(list(self.images.rglob("cam1_*.jpg"))), 1)  # the blocked picture was still saved


class Pinning(unittest.TestCase):
    def setUp(self):
        self.url, self.handshakes = fake_rtsps_camera(self)

    def test_pin_reads_the_certificate_and_its_name(self):
        pinned = web.pin(self.url)
        self.assertEqual(pinned["cert_host"], "camera.test")
        self.assertEqual(ssl.PEM_cert_to_DER_cert(pinned["cert"]), ssl.PEM_cert_to_DER_cert(CAMERA_CERT))
        for nowhere in ("rtsps://127.0.0.1:1/live", "rtsps:///live", "rtsps://127.0.0.1:99999/live"):
            with self.assertRaises((OSError, ValueError)):
                web.pin(nowhere)

    def test_ffmpeg_accepts_only_the_stored_certificate(self):
        # The listener is no camera, so every grab fails; what differs is whether ffmpeg trusted it enough to talk.
        with self.assertRaises(GrabError) as trusted:
            grab_frame(self.url, timeout=3, cert=CAMERA_CERT, cert_host="camera.test")
        self.assertNotIn("certificate verify failed", str(trusted.exception))
        self.assertEqual(len(self.handshakes), 1)
        for cert, host in ((OTHER_CERT, "camera.test"), (CAMERA_CERT, "elsewhere.test")):
            with self.assertRaises(GrabError) as refused:
                grab_frame(self.url, timeout=3, cert=cert, cert_host=host)
            self.assertIn("certificate verify failed", str(refused.exception), host)
        self.assertEqual(len(self.handshakes), 1)

    def test_rtsps_without_a_stored_certificate_never_connects(self):
        with self.assertRaises(GrabError) as caught:
            grab_frame(self.url, timeout=3)
        self.assertIn("not stored yet", str(caught.exception))
        self.assertEqual(self.handshakes, [])


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

    def test_alert_keys_never_reach_the_page(self):
        keys = {"telegram_token": "123456:hunter3", "telegram_chat": "42", "ntfy_url": "https://ntfy.sh/hunter4"}
        self.config.write_text(json.dumps({**GOOD, "alerts": {**GOOD["alerts"], **keys}}))
        body = self.ask("GET", "/")[1].decode()
        self.assertNotIn("hunter", body)
        self.assertIn('value="42"', body)
        self.assertIn(f'name="telegram_token" value="{web.MASK}"', body)
        form = {**FORM, "telegram_token": [web.MASK], "telegram_chat": ["42"], "ntfy_url": [web.MASK]}
        self.assertEqual(self.ask("POST", "/", form)[0], 303)
        self.assertEqual({k: json.loads(self.config.read_text())["alerts"][k] for k in keys}, keys)
        # A refused save shows what was typed, so that nothing typed is lost, and still hides what is stored.
        status, body, _ = self.ask("POST", "/", {**form, "pushover_user": ["typed1"], "times": ["nonsense"]})
        self.assertEqual(status, 400)
        self.assertNotIn(b"hunter", body)
        self.assertIn(b'value="typed1"', body)
        status, body, _ = self.ask("POST", "/", {**form, "telegram_chat": [""]})
        self.assertEqual(status, 400)
        self.assertIn(b"Telegram needs both", body)
        self.assertNotIn(b"hunter", body)

    def test_page_shows_the_latest_picture_and_no_password(self):
        status, body, r = self.ask("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"hunter2", body)
        self.assertIn(b"Latest picture: 2026-11-05 at 08:00", body)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertIn(f"script-src {web.csp_hash(web.SCRIPT)}", r.getheader("Content-Security-Policy"))
        self.assertEqual(body.count(b"<script>"), 1)  # only the hashed one that opens the model comparison
        self.assertEqual(self.ask("GET", "/snapshot/cam1")[:2], (200, b"jpeg bytes"))

    def test_page_reports_how_the_last_runs_went(self):
        logs = Path(self.tmp.name, "logs")
        logs.mkdir()
        self.addCleanup(setattr, web, "LOGS", web.LOGS)
        web.LOGS = logs
        self.assertEqual(self.ask("GET", "/")[0], 200)  # no run yet: nothing to report, and no trouble
        (logs / "grab.log").write_text("=== 2026-11-04 16:00:02 grab exit=0\nsaved /images/cam1/x.jpg\n"
                                       "=== 2026-11-05 08:00:09 grab exit=1\nsaved /images/cam1/y.jpg\n"
                                       "PROBLEM: cam1: no usable frame <b>after</b> 3 attempts\n")
        (logs / "stitch.log").write_text("=== 2026-11-05 00:30:41 stitch exit=0\nbuilt /videos/cam1/x.mp4\n")
        body = self.ask("GET", "/")[1].decode()
        self.assertIn("The last picture run (2026-11-05 at 08:00) had problems:<ul><li>cam1: no usable frame "
                      "&lt;b&gt;after&lt;/b&gt; 3 attempts</li></ul>", body)
        self.assertIn("Last video build: 2026-11-05 at 00:30, no problems.", body)
        self.assertNotIn("saved /images", body)
        # a failure without a PROBLEM line (Docker itself failed) shows the end of what was printed
        (logs / "grab.log").write_text("=== 2026-11-05 12:00:01 grab exit=1\nCannot connect to the Docker daemon\n")
        self.assertIn("<li>Cannot connect to the Docker daemon</li>", self.ask("GET", "/")[1].decode())

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
        status, _, r = self.ask("POST", "/", {**FORM, "url": [web.mask(SECRET), ""], "yearly_fps": ["12"]})
        self.assertEqual((status, r.getheader("Location")), (303, "/?saved"))
        saved = json.loads(self.config.read_text())
        self.assertEqual((saved["cameras"][0]["url"], saved["levels"]["yearly"]["fps"]), (SECRET, 12))
        self.assertIn(b"Settings saved", self.ask("GET", "/?saved")[1])

    def test_rejected_save_changes_nothing_and_says_why(self):
        before = self.config.read_text()
        status, body, _ = self.ask("POST", "/", {**FORM, "times": ["25:00"]})
        self.assertEqual(status, 400)
        self.assertIn(b"Not saved", body)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.ask("POST", "/", FORM, **{"Content-Length": "x"})[0], 400)

    def test_unreadable_config_still_gives_a_usable_page(self):
        for junk in ("{not json", "[1, 2]", json.dumps({**GOOD, "cameras": [7]}), json.dumps({**GOOD, "view_model": []})):
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

    def test_saving_an_rtsps_camera_stores_its_certificate(self):
        url, _ = fake_rtsps_camera(self)
        self.assertEqual(self.ask("POST", "/", {**FORM, "url": [url, ""]})[0], 303)
        camera = json.loads(self.config.read_text())["cameras"][0]
        self.assertEqual((camera["url"], camera["cert_host"]), (url, "camera.test"))
        self.assertIn(b"certificate is stored", self.ask("GET", "/")[1])
        # a camera that cannot be asked for its certificate is not saved without one
        status, body, _ = self.ask("POST", "/", {**FORM, "url": ["rtsps://127.0.0.1:1/live", ""]})
        self.assertEqual(status, 400)
        self.assertIn(b"certificate could not be read", body)
        self.assertEqual(json.loads(self.config.read_text())["cameras"][0], camera)

    def test_preview_shows_the_look_on_the_latest_picture_without_saving(self):
        before = self.config.read_text()
        form = {**FORM, "url": [web.mask(SECRET), ""], "shape": ["1:1"], "stamp": ["datetime"], "blend": ["strong"],
                "deflicker": ["on"], "action": ["preview"]}
        self.assertIn(b"The preview could not be made", self.ask("POST", "/", form)[1])  # the stand-in is no JPEG
        jpeg(self.shot)
        body = self.ask("POST", "/", form)[1].decode()
        self.assertIn("Preview made. Nothing was saved.", body)
        picture = base64.b64decode(body.split('src="data:image/jpeg;base64,')[1].split('"')[0])
        self.assertEqual(t.probe(picture), {"nb_read_frames": 1, "width": 240, "height": 240})  # 320x240, squared
        self.assertEqual(self.config.read_text(), before)
        # a preview of settings that would not be saved says why instead
        self.assertIn(b"Not saved", self.ask("POST", "/", {**form, "size": ["huge"]})[1])

    def test_camera_test_shows_the_live_picture(self):
        old = web.grab_frame
        self.addCleanup(lambda: setattr(web, "grab_frame", old))
        web.grab_frame = lambda url, quality, **kw: b"frame" if url == SECRET else self.fail(url)
        # the saved address comes back masked, and the test still reaches the camera with the stored password
        body = self.ask("POST", "/", {**FORM, "url": [web.mask(SECRET), ""], "action": ["test"]})[1]
        self.assertIn(b'src="data:image/jpeg;base64,ZnJhbWU="', body)
        self.assertNotIn(b"hunter2", body)


class TimeLimit(unittest.TestCase):
    def test_a_job_that_runs_too_long_is_stopped_and_says_so(self):
        waits = "import select, timelapse as t; t.time_limit('grab', 1); select.select([], [], [], 5)"
        r = subprocess.run([sys.executable, "-c", waits], capture_output=True, text=True, cwd=Path(__file__).parent)
        self.assertEqual((r.returncode, r.stdout), (1, "PROBLEM: the grab job ran too long and was stopped\n"))


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
        out, note = t.snapshot(lambda url, quality, **pin: src.read_bytes(), GOOD["cameras"][0], 95, self.images)
        self.assertIsNone(note)
        self.assertEqual(out.parent.parent.name, "cam1")
        exif = piexif.load(str(out))
        self.assertEqual(exif["0th"][piexif.ImageIFD.Make], b"Wyze")
        self.assertRegex(exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal].decode(), r"^[+-]\d\d:\d\d$")
        self.assertEqual(exif["Exif"][piexif.ExifIFD.PixelXDimension], 320)
        self.assertEqual(t.probe(out)["nb_read_frames"], 1)

        t.time.sleep = lambda s: None
        for frame in (b"not a jpeg", src.read_bytes()[:3000]):
            with self.assertRaises(RuntimeError):
                t.snapshot(lambda url, quality, **pin: frame, GOOD["cameras"][0], 95, self.images)
        self.assertEqual(len(list(self.images.rglob("cam1_*"))), 1)  # nothing bad or half-written left behind

    def test_a_picture_is_filed_under_the_time_its_run_started(self):
        src = self.images / "src.jpg"
        jpeg(src)
        started = datetime(2026, 1, 5, 23, 59, 58).astimezone()
        out, _ = t.snapshot(lambda url, quality, **pin: src.read_bytes(), GOOD["cameras"][0], 95, self.images,
                            when=started)
        self.assertEqual(out, self.images / "cam1" / "2026-01-05" / "cam1_2026-01-05_23-59-58.jpg")
        taken = piexif.load(str(out))["Exif"][piexif.ExifIFD.DateTimeOriginal].decode()
        self.assertNotIn("2026:01:05", taken)  # the picture itself keeps the moment it was taken

    def test_cameras_are_grabbed_together_under_the_time_the_run_started(self):
        src = self.images / "src.jpg"
        jpeg(src)
        together = threading.Barrier(2, timeout=2)

        def slow_camera(url, quality, **pin):
            together.wait()  # passes only while both cameras are being grabbed
            threading.Event().wait(1.5)
            return src.read_bytes()

        old = t.grab_frame
        self.addCleanup(lambda: setattr(t, "grab_frame", old))
        t.grab_frame = slow_camera
        before = datetime.now()
        cameras = [{"name": name, "url": "rtsp://10.0.0.5:8554/live"} for name in ("cam1", "cam2")]
        self.assertEqual(t.grab({**GOOD, "cameras": cameras}, self.images), [])
        for name in ("cam1", "cam2"):
            saved = next(self.images.glob(f"{name}/*/{name}_*.jpg"))
            named = datetime.strptime(saved.stem[len(name) + 1:], "%Y-%m-%d_%H-%M-%S")
            self.assertLess((named - before).total_seconds(), 0.5, saved.name)  # not the 1.5 seconds later it was saved

    def test_grab_error_never_leaks_the_camera_password(self):
        with self.assertRaises(GrabError) as caught:
            grab_frame("rtsp://admin:hunter2@127.0.0.1:1/live", timeout=3)
        self.assertNotIn("hunter2", str(caught.exception))

    def fill(self, first, last, hours=("08", "12")):
        day, n = first, 0
        while day <= last:
            for hour in hours:
                jpeg(self.images / "cam1" / day.isoformat() / f"cam1_{day}_{hour}-00-00.jpg", n)
                n += 1
            day += timedelta(days=1)

    def names(self, level):
        return sorted(p.name for p in (self.videos / "cam1" / level).iterdir())

    def test_stitch_builds_every_kind_of_video_and_reports_problems(self):
        # two snapshots a day from Mon 2025-12-01 to Tue 2026-01-06, "today" is Mon 2026-02-02
        self.fill(date(2025, 12, 1), date(2026, 1, 6))
        (self.images / "cam1" / "2025-12-03" / "IMG_0001.jpg").write_bytes(b"dropped in by hand, not a snapshot")
        today = date(2026, 2, 2)
        self.assertEqual(t.stitch(GOOD, today, self.images, self.videos),
                         ["cam1: only 0 of 2 snapshots were taken on 2026-02-01"])
        self.assertEqual(len(self.names("daily")), 37)
        self.assertEqual(self.names("weekly"), [f"cam1_2025-W{w}.mp4" for w in (49, 50, 51, 52)]
                         + ["cam1_2026-W01.mp4", "cam1_2026-W02.mp4"])
        self.assertEqual(self.names("monthly"), ["cam1_2025-12.mp4", "cam1_2026-01.mp4"])
        self.assertEqual(self.names("yearly"), ["cam1_2025.mp4"])  # 2026 has not ended
        # each video holds every picture of its own period
        self.assertEqual(t.probe(self.videos / "cam1" / "yearly" / "cam1_2025.mp4")["nb_read_frames"], 31 * 2)
        self.assertEqual(t.probe(self.videos / "cam1" / "weekly" / "cam1_2026-W01.mp4")["nb_read_frames"], 7 * 2)
        self.assertEqual(t.probe(self.videos / "cam1" / "monthly" / "cam1_2026-01.mp4")["nb_read_frames"], 6 * 2)

        # a second run is a no-op apart from the same warning
        before = {p: p.stat().st_mtime_ns for p in self.videos.rglob("*.mp4")}
        t.stitch(GOOD, today, self.images, self.videos)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.videos.rglob("*.mp4")})

    def test_no_missed_snapshot_alert_for_a_day_on_which_the_settings_changed(self):
        self.fill(date(2026, 1, 5), date(2026, 1, 6), hours=("08",))  # one picture a day where GOOD asks for two
        saved = Path(self.tmp.name, "config.json")
        saved.touch()

        def alerts(changed):
            os.utime(saved, (changed.timestamp(), changed.timestamp()))
            return t.stitch(GOOD, date(2026, 1, 7), self.images, self.videos, saved)

        self.assertEqual(alerts(datetime(2026, 1, 6, 15)), [])  # the second picture time was added that afternoon
        self.assertEqual(alerts(datetime(2026, 1, 5, 15)), ["cam1: only 1 of 2 snapshots were taken on 2026-01-06"])

    def test_each_kind_of_video_has_its_own_speed_window_and_switch(self):
        self.fill(date(2025, 12, 29), date(2025, 12, 31), hours=("08", "12", "20"))
        cfg = changed(daily={"make": False}, weekly={"make": False}, monthly={"fps": 3},
                      yearly={"from": "11:00", "to": "13:00", "fps": 5})
        t.stitch(cfg, date(2026, 2, 2), self.images, self.videos)
        self.assertEqual(sorted(p.name for p in (self.videos / "cam1").iterdir()), ["monthly", "yearly"])
        month, year = self.videos / "cam1" / "monthly" / "cam1_2025-12.mp4", self.videos / "cam1" / "yearly" / "cam1_2025.mp4"
        self.assertEqual((t.probe(month)["nb_read_frames"], stream(month, "r_frame_rate")), (9, "3/1"))
        self.assertEqual((t.probe(year)["nb_read_frames"], stream(year, "r_frame_rate")), (3, "5/1"))  # midday only

    def test_every_look_option_at_once(self):
        self.fill(date(2026, 1, 5), date(2026, 1, 7))
        everything = {"size": "720", "shape": "1:1", "turn": "right", "mirror": True, "format": "h265",
                      "quality": "small", "deflicker": True, "blend": "strong", "denoise": True, "sharpen": True,
                      "brightness": 10, "contrast": 120, "saturation": 150, "stamp": "datetime"}
        self.assertEqual(set(everything), set(t.VIDEO))
        self.assertEqual(t.stitch(changed(everything), date(2026, 1, 8), self.images, self.videos), [])
        day = self.videos / "cam1" / "daily" / "cam1_2026-01-06.mp4"
        self.assertEqual(t.probe(day), {"nb_read_frames": 2, "width": 240, "height": 240})  # 320x240 turned and squared
        self.assertEqual(stream(day, "codec_name,codec_tag_string"), "hevc,hvc1")
        for choices in ({"turn": "left", "shape": "9:16"}, {"turn": "upside-down", "shape": "16:9", "size": "2160"},
                        {"quality": "medium", "blend": "light", "stamp": "date", "shape": "4:3"}):
            out = self.videos / "one.mp4"
            t.build(sorted(self.images.rglob("*.jpg")), out, 12, {**t.VIDEO, **choices}, "one")
            self.assertEqual(t.probe(out)["nb_read_frames"], 6, choices)
            out.unlink()

    def test_stamp_is_drawn_even_when_pictures_are_blended(self):
        self.fill(date(2026, 1, 5), date(2026, 1, 6))
        shots = sorted(self.images.rglob("*.jpg"))

        def corner(stamp):  # bottom-left of the last frame, where the stamp goes
            out = self.videos / f"{stamp}.mp4"
            t.build(shots, out, 12, {**t.VIDEO, "blend": "light", "stamp": stamp}, stamp)
            return subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "reverse,crop=iw/2:ih/6:0:ih*5/6",
                                   "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                  capture_output=True, check=True).stdout

        plain = corner("off")
        self.assertEqual(plain, corner("off"))  # the encode is repeatable, so any difference below is the stamp
        differing = sum(a != b for a, b in zip(plain, corner("datetime")))
        self.assertGreater(differing, len(plain) // 50)

    def test_corrupt_picture_is_named_and_other_videos_still_build(self):
        for d in ("2026-01-05", "2026-01-06"):
            jpeg(self.images / "cam1" / d / f"cam1_{d}_08-00-00.jpg")
        bad = self.images / "cam1" / "2026-01-06" / "cam1_2026-01-06_12-00-00.jpg"
        bad.write_bytes(next(self.images.rglob("*.jpg")).read_bytes()[:3000])
        problems = t.stitch({**GOOD, "times": ["08:00"]}, date(2026, 2, 2), self.images, self.videos)
        # the day, the week and the month holding the bad picture, plus the missed-snapshot warning
        self.assertEqual(len(problems), 4, problems)
        self.assertIn("cam1_2026-01-06.mp4", problems[0])
        self.assertIn(f"{bad.name} is corrupt", problems[0])  # names the file to delete
        self.assertEqual(self.names("daily"), ["cam1_2026-01-05.mp4"])
        self.assertEqual(list(self.videos.rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
