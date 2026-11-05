"""Config editor for the timelapse jobs. compose.yaml publishes it on 127.0.0.1 only."""
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from timelapse import CONFIG, read_config, validate

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wyze timelapse settings</title>
<style>
  :root {{ color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }}
  body {{ max-width: 42rem; margin: 2rem auto; padding: 0 1rem; }}
  label {{ display: block; font-weight: 600; margin-top: 1.25rem; }}
  small {{ display: block; font-weight: 400; opacity: .75; }}
  input, textarea {{ width: 100%; box-sizing: border-box; font: inherit; padding: .5rem; margin-top: .25rem; }}
  textarea {{ font-family: ui-monospace, monospace; font-size: .875rem; white-space: pre; }}
  button {{ font: inherit; font-weight: 600; padding: .5rem 1.5rem; margin-top: 1.5rem; }}
  [role=status], [role=alert] {{ padding: .75rem 1rem; border-left: 4px solid; }}
  [role=status] {{ border-color: #2e7d32; }}
  [role=alert] {{ border-color: #c62828; }}
</style>
</head>
<body>
<h1>Wyze timelapse settings</h1>
{message}
<form method="post">
  <label for="cameras">Cameras
    <small>One per line: a short name (lowercase letters, numbers, dashes), a space, then the camera's RTSP address.
    In the Wyze app it is under the camera's Settings, Advanced Settings, RTSP.
    Example: front-yard rtsp://user:pass@192.168.1.50:8554/live</small>
  </label>
  <textarea id="cameras" name="cameras" rows="4" required spellcheck="false">{cameras}</textarea>

  <label for="times">Snapshot times
    <small>24-hour HH:MM, separated by commas.</small>
  </label>
  <input id="times" name="times" value="{times}" required>

  <label for="stitch_time">Video build time
    <small>When each night's videos are built (24-hour HH:MM). Pick a time after midnight.</small>
  </label>
  <input id="stitch_time" name="stitch_time" value="{stitch_time}" required>

  <label for="fps">Frames per second
    <small>How many snapshots play per second of video.</small>
  </label>
  <input id="fps" name="fps" type="number" min="1" max="60" value="{fps}" required>

  <label for="jpeg_quality">Snapshot JPEG quality
    <small>1 to 100. Higher is sharper and larger.</small>
  </label>
  <input id="jpeg_quality" name="jpeg_quality" type="number" min="1" max="100" value="{jpeg_quality}" required>

  <button>Save</button>
</form>
</body>
</html>
"""


def from_form(form):
    def number(key):
        value = form.get(key, [""])[0].strip()
        return int(value) if value.isdigit() else value

    cameras = []
    for line in form.get("cameras", [""])[0].splitlines():
        if line.strip():
            name, _, url = line.strip().partition(" ")
            cameras.append({"name": name, "url": url.strip()})
    return {
        "cameras": cameras,
        "times": [t.strip() for t in form.get("times", [""])[0].split(",") if t.strip()],
        "stitch_time": form.get("stitch_time", [""])[0].strip(),
        "fps": number("fps"),
        "jpeg_quality": number("jpeg_quality"),
    }


def render(cfg, message=""):
    return PAGE.format(
        message=message,
        cameras=html.escape("\n".join(f"{c.get('name', '')} {c.get('url', '')}" for c in cfg.get("cameras", []))),
        times=html.escape(", ".join(cfg.get("times", []))),
        stitch_time=html.escape(str(cfg.get("stitch_time", ""))),
        fps=html.escape(str(cfg.get("fps", ""))),
        jpeg_quality=html.escape(str(cfg.get("jpeg_quality", ""))),
    )


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        # DNS-rebinding guard: the page shows camera passwords, so only answer when addressed as this machine.
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("localhost", "127.0.0.1") or self.path != "/":
            self.send_error(404)
            return False
        return True

    def do_GET(self):
        if self.allowed():
            self.reply(200, render(read_config()))

    def do_POST(self):
        if not self.allowed():
            return
        # CSRF guard: another site open in the same browser must not be able to rewrite the config.
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self.send_error(403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self.send_error(413)
        cfg = from_form(parse_qs(self.rfile.read(length).decode("utf-8", "replace")))
        errors = validate(cfg)
        if errors:
            items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
            return self.reply(400, render(cfg, f'<div role="alert">Not saved:<ul>{items}</ul></div>'))
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, CONFIG)
        self.reply(200, render(cfg, '<div role="status">Saved. The schedule updates itself within a few seconds.</div>'))


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
