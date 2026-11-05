"""Grab one still image from an RTSP stream.

    python rtsp_grabber.py rtsp://address:port/live out.jpg [quality 1-100]

(rtsps cameras need their stored certificate, so they are grabbed through timelapse.py or the settings page.)

A rewrite of jcriger/rtsp-grabber (MIT, last updated 2021), which this project started from. What changed:
- ffmpeg does the capture instead of OpenCV, so the only requirement is an ffmpeg binary (no opencv, numpy
  or dateutil). ffmpeg waits for the first keyframe, so the half-decoded grey frames the old
  "first frame OpenCV hands back" approach could save do not happen.
- Every network step has a timeout; a dead camera fails in seconds instead of hanging.
- Failures raise GrabError with the reason, and the reason never contains the URL's username or password.
- One frame per call. The endless loop and config.txt are gone: scheduling belongs to launchd/cron.
"""
import re
import subprocess
import sys
import tempfile


class GrabError(Exception):
    pass


def grab_frame(url, quality=95, timeout=20, cert=None, cert_host=None):
    """Return one JPEG frame as bytes. `quality` is 1-100, `timeout` is seconds per network operation.

    An rtsps camera must come with `cert`, the certificate (PEM) stored when the camera was added, and
    `cert_host`, a name that certificate is issued to. Cameras sign their own certificates, so the stored one
    is the only authority accepted, and it is checked against its own name because the address is usually
    a bare IP that the certificate does not mention.
    """
    qscale = round(31 - (quality - 1) * 29 / 99)  # ffmpeg's JPEG scale runs from 31 (worst) to 2 (best)
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as pinned:
        tls = []
        if url.startswith("rtsps:"):
            if not (cert and cert_host):
                raise GrabError("this camera's certificate is not stored yet: open the settings page and press "
                                "Save settings")
            pinned.write(cert)
            pinned.flush()
            tls = ["-ca_file", pinned.name, "-verifyhost", cert_host]
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-rtsp_transport", "tcp", "-timeout", str(timeout * 1_000_000),
               *tls, "-i", url, "-frames:v", "1", "-q:v", str(qscale), "-c:v", "mjpeg", "-f", "image2pipe", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout * 2)
        except subprocess.TimeoutExpired:
            raise GrabError(f"no frame within {timeout * 2} seconds") from None
    if r.returncode or not r.stdout:
        reason = r.stderr.decode(errors="replace").replace(url, "<camera url>")
        reason = re.sub(r"//[^/@\s]+@", "//***@", reason).strip()
        raise GrabError(reason[-300:] or "the stream produced no frame")
    return r.stdout


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    try:
        frame = grab_frame(sys.argv[1], *(int(q) for q in sys.argv[3:]))
    except GrabError as e:
        sys.exit(f"grab failed: {e}")
    with open(sys.argv[2], "wb") as f:
        f.write(frame)
