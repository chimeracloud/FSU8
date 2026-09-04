"""
Configuration — settings and GCS persistence.

ADAPTED from FSU1B's `core/config.py` + `core/gcs_config.py`, merged
into one module because the shell specification lists `core/config.py`
as the place settings live and does not list a second file.

Per CHI-POL-006: portal-editable config lives in GCS, never in env
vars. Env vars carry deploy-time identity only (`SERVICE_URL`,
`GCP_PROJECT`) — never a tunable setting.

Live Betting Control holds **no credentials**. There is no SecretRefs
block here and there must never be one: the service calls FSU1B over
HTTP with an IAM ID token minted from its own Cloud Run identity, and
FSU1B is the only thing that holds a Betfair session.

Failure mode:
  GCS unreachable on startup -> log a warning and serve in-memory
  defaults, so an operator can still reach the admin surface and
  intervene. A failed save bubbles up to `PUT /admin/config` as a 502
  with the in-memory change already applied, so the operator knows to
  retry rather than assuming it stuck.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields, replace
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    # ── Identity ────────────────────────────────────────────────────
    service_name: str = "fsu1bv2-betting-control"
    region: str = "europe-west2"
    gcp_project: str = "chiops"

    # ── Lifecycle ───────────────────────────────────────────────────
    # Post-incident hardening: the service boots stopped and an
    # operator starts it deliberately. The GCS blob governs — the
    # default below is decorative once a blob exists.
    auto_start: bool = False

    # ── Upstream gateway ────────────────────────────────────────────
    # The ONLY outbound dependency. Empty until the FSU1B URL is set,
    # which /ready reports as not-ready rather than guessing.
    fsu1b_url: str = ""
    fsu1b_timeout_s: float = 10.0

    # ── Operational ─────────────────────────────────────────────────
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR

    # ── Deploy-time identity, injected via env, mirrored here ───────
    service_url: str = ""

    # ── GCS ─────────────────────────────────────────────────────────
    config_bucket: str = "chiops-betfair-recording"
    config_blob: str = "config/betting_control.json"
    manifest_bucket: str = "chimera-portal-config"
    manifest_blob: str = "source_manifest.json"

    # ── Events (Bible §20) ──────────────────────────────────────────
    events_topic: str = "chimera-events"


_lock = RLock()
_current: Settings = Settings()


def get_settings() -> Settings:
    with _lock:
        return _current


def replace_settings(**changes) -> Settings:
    global _current
    with _lock:
        _current = replace(_current, **changes)
        return _current


def reset_settings_for_test() -> None:
    """Test-only — restore defaults so tests don't leak state."""
    global _current
    with _lock:
        _current = Settings()


def settings_to_dict() -> dict[str, Any]:
    """Serialise to a JSON-safe dict for GCS persistence."""
    with _lock:
        d = asdict(_current)
    for k, v in list(d.items()):
        if isinstance(v, tuple):
            d[k] = list(v)
    return d


def apply_dict(payload: dict[str, Any]) -> Settings:
    """Hydrate from a (possibly partial) dict. Unknown keys are ignored."""
    global _current
    with _lock:
        allowed = {f.name for f in fields(Settings)}
        changes: dict[str, Any] = {}
        for k, v in payload.items():
            if k not in allowed:
                continue
            current_value = getattr(_current, k)
            if isinstance(current_value, tuple) and isinstance(v, list):
                changes[k] = tuple(v)
            else:
                changes[k] = v
        _current = replace(_current, **changes)
        return _current


# ── GCS persistence ─────────────────────────────────────────────────


def _disabled() -> bool:
    """When set, all GCP I/O is skipped — used by tests and local dev."""
    return bool(os.environ.get("BC_DISABLE_GCP_IO"))


def load_config_from_gcs() -> dict[str, Any]:
    """Hydrate settings from GCS. Returns the dict that was applied.

    Absent blob -> write the current defaults and return them.
    Unreachable GCS -> warn and keep in-memory defaults.
    """
    if _disabled():
        logger.info("BC_DISABLE_GCP_IO set — skipping GCS config load.")
        return settings_to_dict()

    s = get_settings()
    bucket_name, blob_name = s.config_bucket, s.config_blob
    try:
        from google.cloud import storage  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("GCS client unavailable (%s) — using in-memory defaults.", exc)
        return settings_to_dict()

    try:
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_name)
        if blob.exists():
            apply_dict(json.loads(blob.download_as_text()))
            logger.info("Config loaded from gs://%s/%s", bucket_name, blob_name)
            return settings_to_dict()
        defaults = settings_to_dict()
        blob.upload_from_string(
            json.dumps(defaults, indent=2), content_type="application/json",
        )
        logger.info(
            "Config absent — wrote defaults to gs://%s/%s", bucket_name, blob_name,
        )
        return defaults
    except Exception as exc:  # noqa: BLE001
        logger.warning("Config load failed (%s) — using in-memory defaults.", exc)
        return settings_to_dict()


def save_config_to_gcs(payload: dict[str, Any]) -> bool:
    """Apply the payload in memory, then persist. True on persistence success.

    The in-memory change is applied FIRST and is not rolled back on a
    write failure — the operator retries the PUT to re-persist, and the
    502 response tells them the change took effect but did not stick.
    """
    apply_dict(payload)

    if _disabled():
        logger.info("BC_DISABLE_GCP_IO set — skipping GCS config save.")
        return True

    s = get_settings()
    try:
        from google.cloud import storage  # type: ignore[import-not-found]

        client = storage.Client()
        blob = client.bucket(s.config_bucket).blob(s.config_blob)
        blob.upload_from_string(
            json.dumps(settings_to_dict(), indent=2),
            content_type="application/json",
        )
        logger.info("Config persisted to gs://%s/%s", s.config_bucket, s.config_blob)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Config persistence failed: %s", exc)
        return False
