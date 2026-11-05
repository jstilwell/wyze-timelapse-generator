"""Settings page for the timelapse jobs. compose.yaml publishes it on 127.0.0.1 only."""
import base64
import hashlib
import html
import json
import os
import re
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from rtsp_grabber import GrabError, grab_frame
from timelapse import (CHOICES, CONFIG, HHMM, IMAGES, LEVELS, PRICES_AS_OF, RANGES, SWITCHES, URL, VIEW_MODELS,
                       latest, read_config, still, validate, well_formed, with_defaults)

MASK = "••••"  # shown in place of a saved camera password
LOGS = Path("/logs")  # what launchd/run.sh wrote about every run; mounted read-only
RUN = re.compile(r"^=== (\d{4}-\d\d-\d\d) (\d\d:\d\d):\d\d \w+ exit=(\d+)\n", re.M)  # run.sh's line above each run

STYLE = """
:root {
  color-scheme: light dark;
  --paper: #f2f4f1; --surface: #fff; --ink: #17262b; --muted: #55676d; --line: #c9d3d1; --edge: #84959a;
  --accent: #0b5c7a; --on-accent: #fff; --ok: #1f7a4d; --bad: #b42318;
  font: 16px/1.5 "Avenir Next", Avenir, system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #11191c; --surface: #19242a; --ink: #e6edee; --muted: #9babaf; --line: #2c3a40; --edge: #62757d;
    --accent: #6cc1e0; --on-accent: #0c1418; --ok: #6fcf97; --bad: #ff8a80;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); }
main { max-width: 46rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
h1 { font-size: 1.75rem; line-height: 1.2; font-weight: 600; letter-spacing: -.01em; margin: 0; }
h2 { font-size: 1.125rem; font-weight: 600; margin: 0 0 .25rem; }
p { margin: 0; }
section { margin-top: 2.5rem; padding-top: 1.5rem; border-top: 1px solid var(--line); }
.hint, figcaption, .hours { color: var(--muted); font-size: .875rem; }
.hint { max-width: 38rem; }
.runs { margin-top: .5rem; }

label, legend { display: block; font-weight: 600; font-size: .875rem; margin-top: 1rem; padding: 0; }
input, textarea, select {
  display: block; width: 100%; margin-top: .25rem; padding: .5rem .625rem;
  font: inherit; font-variant-numeric: tabular-nums; color: inherit;
  background: var(--surface); border: 1px solid var(--edge); border-radius: 6px;
}
textarea { resize: vertical; }
input[type=checkbox] { display: inline; width: auto; margin: 0 .25rem 0 0; }
label.tick { display: inline; font-weight: 400; margin: 0; }
input[type=time], input[type=number] { max-width: 12rem; }
select { appearance: none; padding-right: 2rem; background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%),
  linear-gradient(135deg, var(--muted) 50%, transparent 50%); background-size: 6px 6px; background-repeat: no-repeat;
  background-position: calc(100% - 17px) 55%, calc(100% - 11px) 55%; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.camera, .level { border: 0; margin: 1.5rem 0 0; padding: 0; min-width: 0; }
.camera legend, .level legend { font-size: 1rem; margin: 0; }
.ticks { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: .5rem 1.25rem; margin-top: 1.25rem; }
.row { display: grid; grid-template-columns: 15rem 1fr; gap: 0 1.25rem; align-items: start; }
.row label:first-child { margin-top: .5rem; }
figure { margin: .5rem 0 0; }
figure img, .empty { display: block; width: 100%; aspect-ratio: 16 / 9; border-radius: 6px; }
figure img { object-fit: cover; background: var(--line); }
figure img.whole { aspect-ratio: auto; }  /* a preview shows its real shape, stamp and all */
.empty { display: grid; place-items: center; padding: 1rem; text-align: center; border: 1px dashed var(--edge); }
figcaption { margin-top: .375rem; }
.ok { color: var(--ok); }
.bad { color: var(--bad); }

.day { display: block; width: 100%; height: 2.75rem; margin-top: 1rem; }
.day line { stroke: var(--ink); stroke-width: 2; vector-effect: non-scaling-stroke; }
.day .build { stroke: var(--accent); stroke-width: 5; }
.hours { display: flex; justify-content: space-between; font-size: .75rem; font-variant-numeric: tabular-nums; }

.fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: 0 1.25rem; align-items: end; }
.fields.wide { grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); }
details { margin-top: 1rem; }
summary { cursor: pointer; color: var(--accent); font-weight: 600; font-size: .875rem; }

.actions { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1rem; }
button {
  font: inherit; font-weight: 600; padding: .625rem 1.25rem; cursor: pointer;
  color: var(--on-accent); background: var(--accent); border: 1px solid var(--accent); border-radius: 6px;
}
button.quiet { color: var(--accent); background: transparent; }
a { color: var(--accent); }
button.link { padding: 0; border: 0; font-size: .875rem; text-decoration: underline; color: var(--accent); background: none; }
dialog { width: min(44rem, calc(100vw - 2rem)); padding: 1.5rem; color: var(--ink); background: var(--paper);
  border: 1px solid var(--line); border-radius: 10px; }
dialog::backdrop { background: rgb(0 0 0 / .55); }
dialog p { margin-top: .75rem; }
table { width: 100%; margin-top: 1rem; border-collapse: collapse; font-size: .875rem; font-variant-numeric: tabular-nums; }
th, td { padding: .625rem .75rem .625rem 0; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
dialog form { margin-top: 1.25rem; }
[role=status], [role=alert] {
  margin-top: 1.5rem; padding: .75rem 1rem; background: var(--surface);
  border-left: 4px solid var(--ok); border-radius: 0 6px 6px 0;
}
[role=alert] { border-left-color: var(--bad); }
[role=alert] ul { margin: .25rem 0 0; padding-left: 1.25rem; }
@media (max-width: 36rem) { .row { grid-template-columns: 1fr; } }
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wyze timelapse settings</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7.5' fill='%230b5c7a'/%3E%3Ccircle cx='8' cy='8' r='3' fill='%23fff'/%3E%3C/svg%3E">
<style>{style}</style>
</head>
<body>
<main>
<h1>Wyze timelapse</h1>
<p class="hint">Takes a picture from each camera at the times below and turns them into daily, weekly, monthly
and yearly videos.</p>
{message}
<form method="post">
  <section>
    <h2>Cameras</h2>
    <p class="hint">The address is in the Wyze app under the camera's Settings, Advanced Settings, RTSP.
    To remove a camera, empty both of its boxes.</p>
    {cameras}
  </section>

  <section>
    <h2>Schedule</h2>
    <p class="hint">Each mark is one picture. The wide mark is when the videos are built.</p>
    {day}
    <label for="times">Picture times</label>
    <textarea id="times" name="times" rows="2" required spellcheck="false"
      aria-describedby="times-hint">{times}</textarea>
    <p class="hint" id="times-hint">24-hour times such as 08:00, separated by commas.</p>
    <details>
      <summary>Add evenly spaced times</summary>
      <div class="fields">
        <div><label for="from">From</label><input id="from" name="from" type="time"></div>
        <div><label for="to">To</label><input id="to" name="to" type="time"></div>
        <div><label for="every">Every (minutes)</label>
          <input id="every" name="every" type="number" min="5" max="720" value="30"></div>
      </div>
      <p class="hint">Saving adds these to the picture times above.</p>
    </details>
    <label for="stitch_time">Build videos at</label>
    <input id="stitch_time" name="stitch_time" type="time" value="{stitch_time}" required
      aria-describedby="stitch-hint">
    <p class="hint" id="stitch-hint">Once a day. Pick a time after midnight, when the day's pictures are complete.</p>
    <label for="jpeg_quality">Picture quality (1 to 100)</label>
    <input id="jpeg_quality" name="jpeg_quality" type="number" min="1" max="100" value="{jpeg_quality}" required
      aria-describedby="quality-hint">
    <p class="hint" id="quality-hint">Higher is sharper and takes more space.</p>
  </section>

  <section>
    <h2>Blocked view check</h2>
    <p class="hint">Claude, Anthropic's AI, can look at each picture as it is taken. If something close to the
    camera is in the way, the picture is taken again half a minute later, up to three more times. Movement further
    back in the scene is left alone.</p>
    <p class="ticks"><label class="tick"><input type="checkbox" name="check_view"{check_view}>Check each picture with
      Claude</label>
      <label class="tick"><input type="checkbox" name="view_compare"{view_compare}>Send the camera's previous
      picture too, to show Claude the usual view</label></p>
    <label for="view_model">Model</label>
    <select id="view_model" name="view_model" aria-describedby="model-hint">{view_models}</select>
    <p class="hint" id="model-hint">{cost_basis} {compare_cost}
      <button type="button" class="link" id="models-open">Compare the models</button></p>
    <label for="view_notes">What Claude should know about your cameras</label>
    <textarea id="view_notes" name="view_notes" rows="4" maxlength="2000" aria-describedby="notes-hint"
      placeholder="The white post on the left of front-yard is always there. Birds on the feeder are fine.">{view_notes}</textarea>
    <p class="hint" id="notes-hint">Optional, and sent with every check. Checking needs an Anthropic API key in the
    .env file (see the README) and sends each picture, shrunk, to Anthropic.</p>
  </section>

  <section>
    <h2>Videos</h2>
    <p class="hint">Each kind of video has its own speed and can use only the pictures taken in part of the day.
    A narrow window, such as 11:00 to 13:00 for the yearly video, stops day and night flashing past.</p>
    {levels}
  </section>

  <section>
    <h2>Look</h2>
    <p class="hint">Applies to videos built from now on. To remake an older video, delete its file and it is
    built again the next night. Preview look shows the result on each camera's latest picture.</p>
    {look}
  </section>

  <section class="actions">
    <button>Save settings</button>
    <button class="quiet" name="action" value="test">Test cameras</button>
    <button class="quiet" name="action" value="preview">Preview look</button>
    <p class="hint">Testing and previewing save nothing. A test takes up to 20 seconds.</p>
  </section>
</form>
<dialog id="models" aria-labelledby="models-title">
  <h2 id="models-title">The three models</h2>
  <p class="hint">All three can tell when something is in front of a camera. They differ in how carefully they
  judge the hard cases, and in price.</p>
  <table>
    <thead><tr><th scope="col">Model</th><th scope="col">What to expect</th><th scope="col">Price per million
      tokens</th><th scope="col">A month, with the previous picture</th><th scope="col">A month, without</th></tr></thead>
    <tbody>{model_rows}</tbody>
  </table>
  <p class="hint">{cost_basis} A check is about {tokens_with:,} tokens in with the previous picture,
  {tokens_without:,} without, and 250 out. These are estimates: the real counts are written to logs/grab.log
  after every check. Retaken pictures are checked again and add a little. Prices are Anthropic's from {prices_as_of} and can change:
  <a href="https://platform.claude.com/docs/en/about-claude/pricing">current prices</a>.</p>
  <p class="hint">If Anthropic's safety filter declines a picture, Opus and Sonnet are tried again on a backup
  model. Haiku is not; the picture is kept and the alert says it was not checked.</p>
  <form method="dialog"><button>Close</button></form>
</dialog>
</main>
<script>{script}</script>
</body>
</html>
"""

SCRIPT = 'document.getElementById("models-open").onclick = () => document.getElementById("models").showModal();'


def csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


HEADERS = {
    "Cache-Control": "no-store",  # the page and the pictures are private to this machine
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; form-action 'self'; base-uri 'none'; "
                               # the page's one style block and one script (it opens the model comparison)
                               f"frame-ancestors 'none'; style-src {csp_hash(STYLE)}; script-src {csp_hash(SCRIPT)}",
    "Cross-Origin-Resource-Policy": "same-origin",
    # same-origin, not no-referrer: that one makes browsers post "Origin: null", which the CSRF guard refuses
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


def mask(url):
    """The address with its password hidden: rtsp://user:secret@host -> rtsp://user:••••@host"""
    return re.sub(r"(//[^/\s:]*):[^/\s]*@", rf"\1:{MASK}@", url)


def endpoint(url):
    """(host, port) of a camera address, the thing a stored certificate belongs to. None if it has neither."""
    try:
        parts = urlsplit(url)
        return parts.hostname and (parts.hostname, parts.port or 322)  # 322: the rtsps default
    except ValueError:
        return None


def pin(url):
    """Trust on first use: the certificate an rtsps camera presents now, and a name it is issued to. Every later
    grab is verified against the two (see grab_frame). Raises OSError or ValueError if the camera cannot be asked."""
    where = endpoint(url)
    if not where:
        raise ValueError("no host in the address")
    cert = ssl.get_server_certificate(where, timeout=8)
    # Connect once more, accepting nothing but that certificate, to read the names it carries. The name check is
    # off here because the name is what is being looked up; the address is usually an IP the certificate lacks.
    only = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    only.check_hostname = False
    only.load_verify_locations(cadata=cert)
    with socket.create_connection(where, timeout=8) as sock, only.wrap_socket(sock) as tls:
        seen = tls.getpeercert()
    names = ([value for kind, value in seen.get("subjectAltName", ()) if kind == "DNS"]
             or [value for part in seen.get("subject", ()) for kind, value in part if kind == "commonName"])
    if not names:
        raise ValueError("the certificate names no host")
    return {"cert": cert, "cert_host": names[0]}


def try_camera(cam):
    """Grab one live frame and return what to show in the camera's picture slot."""
    if not URL.fullmatch(cam["url"]) or MASK in cam["url"]:
        return '<p class="empty bad">Fill in the whole address first.</p>'
    try:
        frame = grab_frame(cam["url"], 80, timeout=8, cert=cam.get("cert"), cert_host=cam.get("cert_host"))
    except GrabError as e:
        reason = str(e)
        hint = ("The camera did not accept the user name or password." if "401" in reason else
                "The camera's certificate could not be read, or is not the one stored for it."
                if "certificate" in reason else
                "Nothing answered at that address. Check it, and that the camera is switched on."
                if re.search("refused|no route|unreachable|timed out|no frame within", reason, re.I) else
                "The camera answered but sent no picture.")
        return f'<p class="empty bad">{hint}</p><figcaption>{html.escape(reason.splitlines()[-1][-160:])}</figcaption>'
    return (f'<img src="data:image/jpeg;base64,{base64.b64encode(frame).decode()}" '
            f'alt="Live picture from {html.escape(cam["name"])}">'
            '<figcaption class="ok">Connected. This is what the camera sees now.</figcaption>')


def preview(cam, video):
    """The look applied to a camera's latest picture, for its picture slot."""
    shot = latest(cam["name"], IMAGES)
    if not shot:
        return '<p class="empty hint">No picture yet to preview. The first one arrives at the next picture time.</p>'
    try:
        frame = still(shot, video)
    except RuntimeError as e:
        return f'<p class="empty bad">The preview could not be made.</p><figcaption>{html.escape(str(e)[-160:])}</figcaption>'
    return (f'<img class="whole" src="data:image/jpeg;base64,{base64.b64encode(frame).decode()}" '
            f'alt="Preview of the look on {html.escape(cam["name"])}">'
            '<figcaption>The look on the latest picture. Flicker and blending only show in videos.</figcaption>')


def camera_rows(cameras, secret, tests):
    rows = []
    for i, cam in enumerate([*cameras, {"name": "", "url": ""}]):
        name, shot = html.escape(cam["name"]), latest(cam["name"], IMAGES)
        if tests and i < len(tests):
            picture = tests[i]
        elif shot:
            taken = shot.stem[len(cam["name"]) + 1:]  # 2026-11-05_08-00-03
            picture = (f'<img src="/snapshot/{name}" alt="Latest picture from {name}"><figcaption>Latest picture: '
                       f'{html.escape(taken[:10])} at {html.escape(taken[11:16].replace("-", ":"))}</figcaption>')
        elif i < len(cameras):
            picture = '<p class="empty hint">No picture yet. Test cameras shows whether the address works.</p>'
        else:
            picture = '<p class="empty hint">Fill in both boxes to add a camera.</p>'
        # A saved address is shown with its password hidden; one typed in this request is shown back as typed.
        url = html.escape(mask(cam["url"]) if cam["url"] in secret else cam["url"])
        pinned = cam.get("cert") and f"""
          <p class="hint">This camera's certificate is stored, and any other is refused.
            <label class="tick"><input type="checkbox" name="repin" value="{i}">The camera was reset: store its
            new certificate</label></p>"""
        rows.append(f"""<fieldset class="camera">
      <legend>{name or "Add a camera"}</legend>
      <div class="row">
        <figure>{picture}</figure>
        <div>
          <label for="name-{i}">Name</label>
          <input id="name-{i}" name="name" value="{name}" placeholder="front-yard" maxlength="32"
            pattern="[a-z0-9][a-z0-9_\\-]*" title="Lowercase letters, numbers, dashes and underscores"
            autocomplete="off" autocapitalize="none" spellcheck="false">
          <label for="url-{i}">Address</label>
          <input id="url-{i}" name="url" value="{url}" placeholder="rtsp://user:password@192.168.1.50/live"
            autocomplete="off" autocapitalize="none" spellcheck="false">{pinned or ""}
        </div>
      </div>
    </fieldset>""")
    return "\n    ".join(rows)


def day_strip(cfg):
    """The schedule on a 24-hour bar: a sky band for orientation, one tick per picture, a wide tick for the build."""
    def minute(t):
        return int(t[:2]) * 60 + int(t[3:])

    times = [t for t in cfg["times"] if HHMM.fullmatch(t)]
    build = cfg["stitch_time"] if HHMM.fullmatch(cfg["stitch_time"]) else ""
    ticks = "".join(f'<line x1="{minute(t)}" x2="{minute(t)}" y1="18" y2="44"/>' for t in times)
    if build:
        ticks += f'<line class="build" x1="{minute(build)}" x2="{minute(build)}" y1="0" y2="44"/>'
    stops = "".join(f'<stop offset="{at}" stop-color="{colour}"/>' for at, colour in (
        (0, "#1d2b4f"), (.2, "#1d2b4f"), (.27, "#e9a15b"), (.35, "#9ccbea"), (.65, "#9ccbea"), (.75, "#e08a5b"),
        (.83, "#1d2b4f"), (1, "#1d2b4f")))
    label = f"Pictures at {', '.join(times) or 'no times yet'}." + (f" Videos are built at {build}." if build else "")
    return (f'<svg class="day" viewBox="0 0 1440 44" preserveAspectRatio="none" role="img" aria-label="{label}">'
            f'<defs><linearGradient id="sky">{stops}</linearGradient></defs>'
            f'<rect width="1440" height="12" fill="url(#sky)"/>{ticks}</svg>'
            '<div class="hours" aria-hidden="true"><span>00:00</span><span>06:00</span><span>12:00</span>'
            '<span>18:00</span><span>24:00</span></div>')


def plays(cfg, level):
    """How long one video of this kind comes out, so a too-short or empty one shows before a month goes by."""
    own = cfg["levels"][level]
    if not own["make"]:
        return "Not made."
    n = sum(own["from"] <= t <= own["to"] for t in cfg["times"])
    if not n or type(own["fps"]) is not int or own["fps"] < 1:
        return "No picture time falls in this window, so nothing would be made."
    seconds = n * {"daily": 1, "weekly": 7, "monthly": 30, "yearly": 365}[level] / own["fps"]
    span = (f"{seconds:.1f} seconds" if seconds < 10 else f"{seconds:.0f} seconds" if seconds < 90
            else f"{seconds / 60:.1f} minutes")
    return f"{n} picture{'s' * (n != 1)} a day: plays for about {span}."


def level_rows(cfg):
    rows = []
    for level in LEVELS:
        own = cfg["levels"][level]
        fps, start, end = (html.escape(str(own.get(key, ""))) for key in ("fps", "from", "to"))
        rows.append(f"""<fieldset class="level">
      <legend><label class="tick"><input type="checkbox" name="{level}_make"{" checked" * (own.get("make") is True)}>Make
        {level} videos</label></legend>
      <div class="fields">
        <div><label for="{level}_fps">Pictures per second</label>
          <input id="{level}_fps" name="{level}_fps" type="number" min="1" max="60" value="{fps}" required></div>
        <div><label for="{level}_from">Use pictures from</label>
          <input id="{level}_from" name="{level}_from" type="time" value="{start}" required></div>
        <div><label for="{level}_to">to</label>
          <input id="{level}_to" name="{level}_to" type="time" value="{end}" required></div>
      </div>
      <p class="hint">{plays(cfg, level)}</p>
    </fieldset>""")
    return "\n    ".join(rows)


LABELS = {
    "size": ("Size", {"original": "Same as the camera", "2160": "4K (2160 lines)", "1080": "Full HD (1080 lines)",
                      "720": "HD (720 lines)"}),
    "shape": ("Shape", {"original": "Same as the camera", "16:9": "Widescreen 16:9", "4:3": "Classic 4:3",
                        "1:1": "Square", "9:16": "Tall 9:16, for phones"}),
    "turn": ("Turn the picture", {"none": "No", "right": "Quarter turn right", "upside-down": "Upside down",
                                  "left": "Quarter turn left"}),
    "format": ("Format", {"h264": "H.264, plays everywhere", "h265": "H.265, about half the size"}),
    "quality": ("Video quality", {"high": "High", "medium": "Medium", "small": "Smallest file"}),
    "blend": ("Blend pictures", {"off": "Off", "light": "A little", "strong": "A lot"}),
    "stamp": ("Stamp in the corner", {"off": "None", "date": "Date", "datetime": "Date and time"}),
    "mirror": "Mirror left to right",
    "deflicker": "Even out flicker between pictures",
    "denoise": "Reduce grain in dark pictures",
    "sharpen": "Sharpen",
    "brightness": "Brightness",
    "contrast": "Contrast",
    "saturation": "Colour strength",
}


def look_controls(video):
    selects = "".join(
        f'<div><label for="{key}">{LABELS[key][0]}</label><select id="{key}" name="{key}">'
        + "".join(f'<option value="{value}"{" selected" * (video.get(key) == value)}>{text}</option>'
                  for value, text in LABELS[key][1].items()) + "</select></div>" for key in CHOICES)
    numbers = "".join(
        f'<div><label for="{key}">{LABELS[key]}</label><input id="{key}" name="{key}" type="number" min="{lo}" '
        f'max="{hi}" value="{html.escape(str(video.get(key, "")))}" required></div>' for key, (lo, hi) in RANGES.items())
    switches = "".join(
        f'<label class="tick"><input type="checkbox" name="{key}"{" checked" * (video.get(key) is True)}>'
        f'{LABELS[key]}</label>' for key in SWITCHES)
    return (f'<div class="fields wide">{selects}</div><div class="ticks">{switches}</div>'
            f'<div class="fields">{numbers}</div><p class="hint">Blending smooths motion between pictures. Brightness '
            'runs from -50 to 50 and 0 changes nothing; contrast and colour strength are percentages, so 100 changes '
            'nothing.</p>')


EXPECT = {"claude-opus-5-5": "The most careful judge, and Anthropic's recommended default. Best when the view is "
                             "cluttered or your notes describe fine distinctions.",
          "claude-sonnet-5-5": "A middle choice: quicker, and half the price of Opus.",
          "claude-haiku-5-5": "The quickest and by far the cheapest. Fine for plain cases such as a person or a car "
                              "right at the lens; more likely to misjudge subtle ones."}


def check_tokens(cfg, compare):
    """Rough tokens sent per check: the instructions and notes, plus about 760 for each picture (1024 pixels wide,
    at about one token per 28 by 28 pixels). `compare` is whether the previous picture goes along."""
    return 450 + len(cfg["view_notes"]) // 4 + 760 * (2 if compare else 1)


def monthly(cfg, model, compare=None):
    """Estimated dollars a month for checking every scheduled picture of every camera with `model`, assuming
    about 250 tokens come back (a little thinking and a one-line verdict). `compare` defaults to the setting."""
    _, dollars_in, dollars_out = VIEW_MODELS[model]
    compare = cfg.get("view_compare") is True if compare is None else compare
    checks = len(cfg["cameras"]) * len(cfg["times"]) * 30
    return checks * (check_tokens(cfg, compare) * dollars_in + 250 * dollars_out) / 1_000_000


def money(dollars):
    if dollars < 0.005:
        return "under 1¢"
    return f"{dollars * 100:.0f}¢" if dollars < 0.995 else f"${dollars:.2f}" if dollars < 20 else f"${dollars:.0f}"


def render(cfg, message="", saved=None, tests=None):
    """The page for a well-formed config. `saved` is the stored camera list when `cfg` is not it (a rejected or
    test submission); `tests` holds one picture slot per camera after Test cameras."""
    secret = {c["url"] for c in (cfg["cameras"] if saved is None else saved)}
    chosen = cfg.get("view_model") if cfg.get("view_model") in VIEW_MODELS else next(iter(VIEW_MODELS))
    return PAGE.format(
        style=STYLE,
        message=message,
        cameras=camera_rows(cfg["cameras"], secret, tests),
        day=day_strip(cfg),
        times=html.escape(", ".join(cfg["times"])),
        stitch_time=html.escape(cfg["stitch_time"]),
        jpeg_quality=html.escape(str(cfg.get("jpeg_quality", ""))),
        check_view=" checked" * (cfg.get("check_view") is True),
        view_compare=" checked" * (cfg.get("view_compare") is True),
        view_models="".join(
            f'<option value="{model}"{" selected" * (cfg.get("view_model") == model)}>{name}: about '
            f'{money(monthly(cfg, model))} a month</option>' for model, (name, _, _) in VIEW_MODELS.items()),
        cost_basis=(f"Estimated for {len(cfg['cameras'])} camera{'s' * (len(cfg['cameras']) != 1)} and "
                    f"{len(cfg['times'])} picture{'s' * (len(cfg['times']) != 1)} a day, as last saved or previewed."),
        compare_cost=(f"On {VIEW_MODELS[chosen][0]} that is about {money(monthly(cfg, chosen, True))} a month with "
                      f"the previous picture and {money(monthly(cfg, chosen, False))} without."),
        prices_as_of=PRICES_AS_OF,
        tokens_with=check_tokens(cfg, True),
        tokens_without=check_tokens(cfg, False),
        model_rows="".join(
            f'<tr><th scope="row">{name}</th><td>{EXPECT[model]}</td><td>${dollars_in:.2f} in<br>${dollars_out:.2f} '
            f'out</td><td>{money(monthly(cfg, model, True))}</td><td>{money(monthly(cfg, model, False))}</td></tr>'
            for model, (name, dollars_in, dollars_out) in VIEW_MODELS.items()),
        script=SCRIPT,
        view_notes=html.escape(cfg["view_notes"]),
        levels=level_rows(cfg),
        look=look_controls(cfg["video"]),
    )


def from_form(form, saved=()):
    """(config, problems) from a posted form. `saved` is the stored camera list: an address that comes back
    still masked gets its stored password, and only for that exact address. An rtsps camera keeps its stored
    certificate while its host and port stay the same and its "store the new one" box is not ticked."""
    def field(key):
        return form.get(key, [""])[0].strip()

    def number(key):
        return int(field(key)) if re.fullmatch(r"-?[0-9]+", field(key)) else field(key)

    errors = []
    stored = {mask(c["url"]): c["url"] for c in saved}
    pins = {endpoint(c["url"]): c for c in saved if c.get("cert") and c.get("cert_host")}
    cameras = []
    for row, (name, url) in enumerate(zip(form.get("name", []), form.get("url", []))):
        cam = {"name": name.strip(), "url": stored.get(url.strip(), url.strip())}
        if not (cam["name"] or cam["url"]):
            continue
        old = pins.get(endpoint(cam["url"]))
        if cam["url"].startswith("rtsps:") and old and str(row) not in form.get("repin", []):
            cam |= {"cert": old["cert"], "cert_host": old["cert_host"]}
        if MASK in cam["url"]:
            errors.append(f"camera {cam['name']!r}: type the password again in place of {MASK}")
        cameras.append(cam)

    times = [t for t in re.split(r"[,\s]+", field("times")) if t]
    start, end, every = field("from"), field("to"), field("every")
    if start or end:
        if HHMM.fullmatch(start) and HHMM.fullmatch(end) and start <= end and every.isdecimal() and int(every) >= 5:
            a, b = (int(t[:2]) * 60 + int(t[3:]) for t in (start, end))
            times = sorted({*times, *(f"{m // 60:02}:{m % 60:02}" for m in range(a, b + 1, int(every)))})
        else:
            errors.append("evenly spaced times: fill in From, a later To, and Every as 5 minutes or more")
    levels = {level: {"make": f"{level}_make" in form, "fps": number(f"{level}_fps"),
                      "from": field(f"{level}_from"), "to": field(f"{level}_to")} for level in LEVELS}
    video = ({key: field(key) for key in CHOICES} | {key: key in form for key in SWITCHES}
             | {key: number(key) for key in RANGES})
    return {"cameras": cameras, "times": times, "stitch_time": field("stitch_time"),
            "jpeg_quality": number("jpeg_quality"), "levels": levels, "video": video,
            "check_view": "check_view" in form, "view_notes": field("view_notes"),
            "view_model": field("view_model"), "view_compare": "view_compare" in form}, errors


def load():
    """(stored config, warning for the page). A file the page cannot show is replaced by the example settings."""
    try:
        cfg = read_config()
        if well_formed(cfg):
            return cfg, ""
    except (OSError, ValueError):
        pass
    return (with_defaults(json.loads(CONFIG.with_name("config.example.json").read_text())),
            '<div role="alert">config.json could not be read, so these are the example settings. '
            'Saving replaces the file.</div>')


def last_runs():
    """How the latest picture run and the latest video build went, for the top of the page: the problems of one
    that failed, a line for one that worked, nothing for one that has not run yet."""
    failed, worked = "", []
    for job, name in (("grab", "picture run"), ("stitch", "video build")):
        try:
            with open(LOGS / f"{job}.log", "rb") as log:
                log.seek(max(0, log.seek(0, os.SEEK_END) - 20000))  # the log only grows; its end is enough
                text = log.read().decode(errors="replace")
        except OSError:
            continue
        runs = list(RUN.finditer(text))
        if not runs:
            continue
        day, at, code = runs[-1].groups()
        said = text[runs[-1].end():].strip().splitlines()
        if code == "0":
            worked.append(f"Last {name}: {day} at {at}, no problems.")
            continue
        # run.sh puts the same lines in the notification: the PROBLEM lines, or the end of the output without any
        lines = [line[len("PROBLEM:"):].strip() for line in said if line.startswith("PROBLEM:")] or said[-3:]
        items = "".join(f"<li>{html.escape(line[:300])}</li>" for line in lines)
        failed += f'<div role="alert">The last {name} ({day} at {at}) had problems:<ul>{items}</ul></div>'
    return failed + (f'<p class="hint runs">{" ".join(worked)}</p>' if worked else "")


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # a stalled client must not hold a thread forever

    def reply(self, status, body=b"", kind="text/html; charset=utf-8", location=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        for name, value in HEADERS.items():
            self.send_header(name, value)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        """The request path, or None once a request that is not addressed to this machine has been answered.
        DNS-rebinding guard: the page can reach the cameras, so it only answers as localhost."""
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("localhost", "127.0.0.1"):
            return self.send_error(404)
        return urlsplit(self.path).path

    def do_GET(self):
        path = self.route()
        if path == "/":
            cfg, message = load()
            if not message and urlsplit(self.path).query == "saved":
                message = '<div role="status">Settings saved. The schedule updates within a few seconds.</div>'
            self.reply(200, render(cfg, message + last_runs()))
        elif path and path.startswith("/snapshot/") and (shot := latest(path[len("/snapshot/"):], IMAGES)):
            self.reply(200, shot.read_bytes(), "image/jpeg")
        elif path:
            self.send_error(404)

    def do_POST(self):
        path = self.route()
        if path != "/":
            return path and self.send_error(404)
        # CSRF guard: another site open in the same browser must not be able to rewrite the config.
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self.send_error(403)
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            return self.send_error(400)
        if int(length) > 65536:
            return self.send_error(413)
        form = parse_qs(self.rfile.read(int(length)).decode("utf-8", "replace"), keep_blank_values=True)
        stored = load()[0]["cameras"]
        cfg, errors = from_form(form, stored)
        for cam in cfg["cameras"]:
            if cam["url"].startswith("rtsps:") and "cert" not in cam:
                try:
                    cam |= pin(cam["url"])
                except (OSError, ValueError):
                    errors.append(f"camera {cam['name']!r}: its certificate could not be read. Check the address "
                                  "and that the camera is switched on.")
        if form.get("action") == ["test"]:
            with ThreadPoolExecutor() as pool:
                tests = list(pool.map(try_camera, cfg["cameras"]))
            return self.reply(200, render(cfg, '<div role="status">Test finished. Nothing was saved.</div>',
                                          stored, tests))
        errors += validate(cfg)
        if form.get("action") == ["preview"] and not errors:
            with ThreadPoolExecutor() as pool:
                tests = list(pool.map(lambda cam: preview(cam, cfg["video"]), cfg["cameras"]))
            return self.reply(200, render(cfg, '<div role="status">Preview made. Nothing was saved.</div>',
                                          stored, tests))
        if not errors:
            try:
                tmp = CONFIG.with_suffix(".tmp")
                tmp.write_text(json.dumps(cfg, indent=2) + "\n")
                os.replace(tmp, CONFIG)
            except OSError as e:
                errors = [f"the settings file could not be written ({e.strerror})"]
        if errors:
            items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
            return self.reply(400, render(cfg, f'<div role="alert">Not saved:<ul>{items}</ul></div>', stored))
        self.reply(303, location="/?saved")  # redirect, so a refresh does not post the form again


if __name__ == "__main__":
    os.umask(0o077)  # config.json holds camera passwords: readable by its owner only
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
