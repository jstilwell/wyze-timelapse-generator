"""Ask Claude whether something is blocking a camera's view. Optional: "Check each picture" on the settings page.

Only the grab job imports this, and only when the check is switched on. The API key comes from ANTHROPIC_API_KEY in
.env and never reaches the settings page.
"""
import base64
import json
import os
import subprocess

import anthropic

from timelapse import VIEW_MODELS

# If a safety classifier declines a request, Anthropic can re-run it on a fallback model it picks. Haiku has no
# such fallback and must not be sent the parameter.
NO_FALLBACK = {"claude-haiku-5-5"}

SYSTEM = """You check pictures from a fixed outdoor camera that takes a few pictures a day for a long-running \
timelapse. Every picture should show the camera's usual scene, so one where something is in the way gets taken again \
a little later.

Say the view is blocked when something close to the camera hides a meaningful part of the scene: a person, animal, \
vehicle or object right in front of the lens, something covering the lens, or an insect, leaf or web on the lens.

Say it is not blocked for anything that belongs in a timelapse: people, animals, vehicles and other movement further \
back in the scene, changes in light, shadows, weather, season or time of day including night, and anything that is \
part of the usual view even when it is near the camera. Rain, fog, glare or a few drops on the lens are weather, not \
an obstruction. When you cannot tell, say it is not blocked: one imperfect frame matters less than a false alarm.

In the reason, name what is in the way in a few plain words. Leave it empty when the view is not blocked."""

VERDICT = {"type": "object", "additionalProperties": False, "required": ["blocked", "reason"],
           "properties": {"blocked": {"type": "boolean"}, "reason": {"type": "string"}}}


class CheckError(Exception):
    """The check could not be made. The message is safe to show: it never holds the key or a picture."""


def picture(src):
    """A JPEG (bytes, or a path) shrunk to at most 1024 pixels wide, as an image block for the API."""
    data = src if isinstance(src, bytes) else src.read_bytes()
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "image2pipe", "-c:v", "mjpeg", "-i", "-", "-vf",
                        "scale='min(1024,iw)':-2", "-q:v", "5", "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg",
                        "-"], input=data, capture_output=True)
    if r.returncode or not r.stdout:
        raise CheckError("the picture could not be prepared for checking")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(r.stdout).decode()}}


def blocked(frame, camera, usual=None, notes="", model=next(iter(VIEW_MODELS))):
    """What is blocking the view in `frame` (JPEG bytes), in a few words, or "" when nothing is.

    `usual` is an earlier picture from the same camera for comparison, `notes` the owner's free text and `model`
    their choice from the settings page. Raises CheckError when no answer could be had; the caller keeps the
    picture in that case.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CheckError("ANTHROPIC_API_KEY is not set in .env")
    content = []
    if usual:
        content += [{"type": "text", "text": "An earlier picture from this camera, showing its usual view. The light "
                                             "and weather may differ:"}, picture(usual)]
    content += [{"type": "text", "text": "The new picture to check:"}, picture(frame)]
    if notes.strip():
        content.append({"type": "text", "text": f"The owner's notes on what is normal for their cameras. This camera "
                                                f"is named {camera}.\n<notes>\n{notes.strip()}\n</notes>"})
    content.append({"type": "text", "text": "Is the view blocked in the new picture?"})
    fallback = {} if model in NO_FALLBACK else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        response = anthropic.Anthropic(timeout=60.0).beta.messages.create(
            model=model,
            max_tokens=2000,  # the answer is one line; the rest is room for the model's thinking
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT}},
            **fallback,
        )
    except anthropic.AuthenticationError:
        raise CheckError("Anthropic did not accept ANTHROPIC_API_KEY") from None
    except anthropic.RateLimitError:
        raise CheckError("Anthropic's rate limit was reached") from None
    except anthropic.APIStatusError as e:
        raise CheckError(f"Anthropic answered {e.status_code}: {str(e.message)[:150]}") from None
    except anthropic.APIConnectionError:
        raise CheckError("Anthropic could not be reached") from None
    print(f"view check for {camera}: {response.model}, {response.usage.input_tokens} tokens in, "
          f"{response.usage.output_tokens} out")
    if response.stop_reason != "end_turn":  # refusal, max_tokens: there is no verdict to read
        raise CheckError(f"Claude gave no verdict ({response.stop_reason})")
    try:
        verdict = json.loads(next(block.text for block in response.content if block.type == "text"))
        if verdict["blocked"] is not True:
            return ""
        return " ".join(str(verdict["reason"]).split())[:200] or "something is in the way"
    except (StopIteration, ValueError, KeyError, TypeError):
        raise CheckError("Claude's answer could not be read") from None
