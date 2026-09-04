"""
Event envelope publisher (Bible §20).

ADAPTED from FSU1B's `services/event_publisher.py`. Same envelope, same
stub-fallback behaviour, different event types and a different topic.

Envelope (locked by Bible §20):

    {
      "envelope": {
        "source":     "fsu1bv2-betting-control",
        "event_type": "control_started",
        "timestamp":  "<UTC iso>",
        "version":    "1.0"
      },
      "payload": { ...event-specific... }
    }

Phase 1 fires lifecycle events only. Bet events belong to Phase 2 and
are not declared here — no placeholder types.

Topic: `Settings.events_topic`, default `chimera-events` per the build
brief. **That topic does not exist in chiops.** The convention in the
project is per-service (`chimera-fsu1b-events`,
`chimera-fsu100v2-events`), so either the shared topic gets created or
this setting points at a per-service one. Until then the stub fallback
below carries the envelopes, so the service runs either way.

Stub fallback:
  If google-cloud-pubsub is unavailable or the publish fails, the
  envelope is logged to stdout (Cloud Logging still captures it) and
  broadcast on the admin SSE channel, so an operator sees it on the
  portal even when Pub/Sub is misconfigured. Publishing must never be
  able to take the service down.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.events import app_state
from core.version import SERVICE_NAME

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = "1.0"

_publisher: Any = None
_topic_path: str | None = None
_publisher_disabled = False


def _disabled() -> bool:
    return bool(os.environ.get("BC_DISABLE_GCP_IO"))


def build_envelope(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "envelope": {
            "source": SERVICE_NAME,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": ENVELOPE_VERSION,
        },
        "payload": payload or {},
    }


def _get_publisher():
    """Lazy-init the Pub/Sub publisher. Returns (publisher, topic_path) or None."""
    global _publisher, _topic_path, _publisher_disabled

    if _publisher_disabled:
        return None
    if _publisher is not None and _topic_path is not None:
        return _publisher, _topic_path

    try:
        from google.cloud import pubsub_v1  # type: ignore[import-not-found]

        s = get_settings()
        publisher = pubsub_v1.PublisherClient()
        _publisher = publisher
        _topic_path = publisher.topic_path(s.gcp_project, s.events_topic)
        logger.info("Pub/Sub publisher ready: %s", _topic_path)
        return _publisher, _topic_path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pub/Sub unavailable (%s) — falling back to stub.", exc)
        _publisher_disabled = True
        return None


async def publish(event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish an envelope. Never raises — returns the envelope sent."""
    envelope = build_envelope(event_type, payload)

    # Always visible to the operator, regardless of Pub/Sub health.
    app_state.add_activity(f"event:{event_type}", json.dumps(envelope["payload"])[:180])
    try:
        await app_state.broadcast("admin", {"event": event_type, **envelope})
    except Exception:  # noqa: BLE001
        pass

    if _disabled():
        logger.info("BC_DISABLE_GCP_IO set — envelope not published: %s", event_type)
        return envelope

    pub = _get_publisher()
    if pub is None:
        logger.info("envelope (stub): %s", json.dumps(envelope))
        return envelope

    publisher, topic_path = pub
    try:
        future = publisher.publish(
            topic_path, json.dumps(envelope).encode("utf-8"),
        )
        message_id = future.result(timeout=10)
        logger.info("published %s to %s (id=%s)", event_type, topic_path, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish failed for %s (%s) — logged instead.", event_type, exc)
        logger.info("envelope (stub): %s", json.dumps(envelope))

    return envelope


def reset_publisher_for_test() -> None:
    global _publisher, _topic_path, _publisher_disabled
    _publisher = None
    _topic_path = None
    _publisher_disabled = False
