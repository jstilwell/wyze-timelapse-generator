# syntax=docker/dockerfile:1
# Static ffmpeg/ffprobe: grabs the snapshots, builds the videos, and validates both.
FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS ffmpeg

FROM ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim@sha256:6ff812e34a0e14b4b8bc9ddf25e77b3e16bac6e0c4d9b67641f40366fa382d4c

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/

COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt

COPY app/ /app/
WORKDIR /app
USER nobody
ENTRYPOINT ["python", "-u", "/app/timelapse.py"]
