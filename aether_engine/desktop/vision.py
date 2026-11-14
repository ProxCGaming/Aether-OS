"""Vision grounding for UIA-blind targets (S20).

The fallback of the fallback's fallback — used ONLY when UI
Automation cannot resolve a target at all (a canvas-drawn
control, a game surface, a custom-rendered app). The flow:

1. Capture one screenshot through ``desktop/capture.py``.
2. Ask the configured vision model (``auxiliary_models["vision"]``,
   the advisor pattern: an explicit ``provider:::model`` spec, no
   fallback to the run provider) where the described control is,
   as normalised coordinates + a confidence.
3. Verify against UIA where possible: the element at the grounded
   point is read back, and a low-confidence answer is accepted
   ONLY when that verification anchors it to a real element whose
   name matches the description.
4. The caller clicks — after re-checking that the verified
   element does not classify into the hard-confirm class (a
   screenshot must never become a route around the close/quit
   gate).

PRIVACY INVARIANTS (the reason this module exists in this shape):

- The screenshot bytes live in local variables only. They are
  never written to disk, never logged, never attached to a chat
  or an audit record. The grounding result carries coordinates
  and a confidence — nothing else survives the call.
- A capture happens only inside a grounding call the policy gate
  already approved (capture mode off refuses before this module
  is reached; ask confirms every time).
- No vision model configured → grounding is honestly
  unavailable, with that reason — never a silent substitution of
  a different model.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aether_engine.desktop.policy import VISION_CONFIDENCE_FLOOR

logger = logging.getLogger("aether_engine.desktop.vision")

GROUNDING_TIMEOUT_SECONDS = 30.0
CAPTURE_TIMEOUT_SECONDS = 10.0

_PROMPT = (
    "You are locating a UI control in a screenshot for an "
    "accessibility fallback. Find the control described below "
    "and answer with ONLY a JSON object, no other text:\n"
    '{{"x": <0-1000 across the screen width>, '
    '"y": <0-1000 down the screen height>, '
    '"confidence": <0.0-1.0, your honest certainty that this '
    "point is the centre of the described control>}}\n"
    "If the control is not visible, answer "
    '{{"x": -1, "y": -1, "confidence": 0}}.\n'
    "Control: {description}"
)


@dataclass
class GroundingResult:
    """The outcome of one grounding attempt. Coordinates are
    screen pixels when ``ok``; ``verified_element`` is the UIA
    element found at the point, when one exists."""

    ok: bool
    reason: str = ""
    x: int | None = None
    y: int | None = None
    confidence: float = 0.0
    verified_element: Any | None = None  # provider.ResolvedTarget


# ---------------------------------------------------------------------
# Vision provider resolution (the advisor seam, vision key)
# ---------------------------------------------------------------------

def default_vision_provider(user_config: Any | None,
                            secret_store: Any) -> Any | None:
    """The configured vision provider, or None.

    Mirrors ``langgraph/advisor.resolve_advisor_provider``: the
    ``auxiliary_models["vision"]`` spec only, ``provider:::model``
    form, NO fallback to the run provider — grounding with an
    unconfigured or unusable vision model is unavailable, never
    silently re-routed."""
    override = ((getattr(user_config, "auxiliary_models", None) or {})
                .get("vision") or "").strip()
    if not override or override == "auto" or not secret_store:
        return None
    p_name, sep, m_name = override.partition(":::")
    if not (sep and p_name and m_name):
        logger.warning(
            "Vision auxiliary override %r is not a "
            "'provider:::model' spec; grounding disabled", override)
        return None
    try:
        from aether_engine.orchestration.core import _create_provider_instance

        return _create_provider_instance(
            p_name, m_name, secret_store, user_config=user_config)
    except Exception as exc:  # noqa: BLE001 - an unusable vision override disables grounding, never breaks a task
        logger.warning(
            "Vision auxiliary override %r unusable, grounding "
            "disabled: %s", override, exc)
        return None


_provider_resolver: Callable[[], Any | None] | None = None


def set_vision_provider_resolver(
    resolver: Callable[[], Any | None] | None,
):
    """Install the live vision-provider resolver (the engine's
    wiring owns config + secret store); returns the previous one,
    the loader seam's N-43 discipline."""
    global _provider_resolver
    previous = _provider_resolver
    _provider_resolver = resolver
    return previous


def resolve_vision_provider() -> Any | None:
    if _provider_resolver is None:
        return None
    try:
        return _provider_resolver()
    except Exception:
        logger.debug("Vision provider resolution failed", exc_info=True)
        return None


# ---------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------

def _png_size(png: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR — the captured bytes
    themselves are the size authority, so no backend-specific
    size call is needed."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def _parse_grounding_reply(text: str) -> tuple[float, float, float] | None:
    """(x_norm, y_norm, confidence) from the model's reply, or
    None when it did not answer in the required shape."""
    match = re.search(r"\{[^{}]*\}", text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        x = float(data["x"])
        y = float(data["y"])
        confidence = float(data.get("confidence") or 0.0)
    except (ValueError, TypeError, KeyError):
        return None
    return x, y, confidence


def _name_matches(element_name: str, description: str) -> bool:
    """Loose anchor check for low-confidence verification: the
    element's stated name and the requested description share a
    meaningful word (either contains the other, or a ≥4-letter
    word of the description appears in the name)."""
    name = (element_name or "").strip().lower()
    wanted = (description or "").strip().lower()
    if not name or not wanted:
        return False
    if name in wanted or wanted in name:
        return True
    return any(
        len(word) >= 4 and word in name
        for word in re.split(r"[^a-z0-9]+", wanted) if word)


async def ground_target(
    description: str,
    *,
    capture_provider: Any | None = None,
    provider: Any | None = None,
    actuator: Any | None = None,
) -> GroundingResult:
    """Ground one described target to screen coordinates.

    Injectable end to end (the tests drive it with a fake capture,
    a fake vision model and a fake actuator); with no injections
    it uses the live seams. Never raises, never persists the
    screenshot.
    """
    if capture_provider is None:
        from aether_engine.desktop.capture import get_capture_provider

        capture_provider = get_capture_provider()
    if actuator is None:
        from aether_engine.desktop import get_desktop_actuator

        actuator = get_desktop_actuator()
    if provider is None:
        provider = resolve_vision_provider()
    if provider is None:
        return GroundingResult(
            ok=False,
            reason=("No vision model is configured "
                    "(Settings → Providers, auxiliary 'vision' "
                    "model), so screenshot grounding is unavailable."))
    available, reason = capture_provider.availability()
    if not available:
        return GroundingResult(ok=False, reason=reason)

    capture = asyncio.ensure_future(
        asyncio.to_thread(capture_provider.capture_screen))
    done, _pending = await asyncio.wait(
        {capture}, timeout=CAPTURE_TIMEOUT_SECONDS)
    if not done:
        return GroundingResult(
            ok=False, reason="The screenshot capture timed out.")
    try:
        png = capture.result()
    except Exception as exc:  # noqa: BLE001 - capture failure is a result
        return GroundingResult(
            ok=False, reason=f"The screenshot capture failed: {exc}")
    if not png:
        return GroundingResult(
            ok=False, reason="The screenshot capture returned nothing.")
    size = _png_size(png)
    if size is None:
        return GroundingResult(
            ok=False,
            reason="The captured screenshot was not a readable PNG.")
    width, height = size

    try:
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": _PROMPT.format(description=description)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]

        async def _ask() -> str:
            parts: list[str] = []
            async for chunk in provider.call_stream(
                    messages, tools=[], node_name="desktop_grounding"):
                if getattr(chunk, "text", None):
                    parts.append(chunk.text)
                if getattr(chunk, "finish_reason", None):
                    break
            return "".join(parts)

        reply = await asyncio.wait_for(
            _ask(), timeout=GROUNDING_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a failed grounding is refused, never guessed
        return GroundingResult(
            ok=False, reason=f"The vision grounding call failed: {exc}")
    finally:
        # The screenshot's last use was the model call above; drop
        # the only reference. Nothing in this module — or anywhere
        # — wrote it anywhere.
        del png

    parsed = _parse_grounding_reply(reply)
    if parsed is None:
        return GroundingResult(
            ok=False,
            reason="The vision model did not answer with usable "
                   "coordinates, so nothing was clicked.")
    x_norm, y_norm, confidence = parsed
    if x_norm < 0 or y_norm < 0 or confidence <= 0:
        return GroundingResult(
            ok=False,
            reason=("The vision model could not find "
                    f"'{description}' on the current screen, so "
                    "nothing was clicked."),
            confidence=max(0.0, confidence))
    x = int(x_norm / 1000.0 * max(1, width - 1))
    y = int(y_norm / 1000.0 * max(1, height - 1))

    verified = None
    try:
        verified = actuator.element_at_point_now(x, y)
    except Exception:  # noqa: BLE001 - verification is best-effort evidence
        verified = None
    element = getattr(verified, "element", None) if verified else None

    if confidence < VISION_CONFIDENCE_FLOOR:
        # Low confidence is never silent: refuse, UNLESS UIA
        # verification anchors the point to a real element whose
        # name matches what was asked for — then the anchor, not
        # the model, is the evidence.
        if element is not None and _name_matches(
                getattr(element, "name", ""), description):
            return GroundingResult(
                ok=True, x=x, y=y, confidence=confidence,
                reason=("Low vision confidence, accepted on UIA "
                        "verification: the element at the point is "
                        f"'{element.name}'."),
                verified_element=verified)
        return GroundingResult(
            ok=False,
            reason=(f"The vision model's confidence was low "
                    f"({confidence:.2f} < {VISION_CONFIDENCE_FLOOR}), "
                    "and no matching UI element verified the point, "
                    "so nothing was clicked."),
            confidence=confidence,
            verified_element=verified)
    return GroundingResult(
        ok=True, x=x, y=y, confidence=confidence,
        verified_element=verified)
