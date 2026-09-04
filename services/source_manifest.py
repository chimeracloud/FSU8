"""
Source Manifest registration (Bible §21).

ADAPTED from FSU1B's `services/source_manifest.py`. Same read-merge-write
against the shared manifest, different entry.

Manifest: gs://chimera-portal-config/source_manifest.json — a dict keyed
by FSU id. Each FSU registers itself on startup; consumers read it to
discover where each service lives.

Phase 1 declares only the endpoints that exist. `/bets` is NOT listed:
advertising an endpoint the service does not serve would make the
manifest lie to any consumer that reads it. It is added when it is
built.

Failure mode:
  Best-effort. GCS unreachable -> warn and continue; the service is
  functional, just not yet discoverable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.version import SERVICE_DESCRIPTION, SERVICE_NAME

logger = logging.getLogger(__name__)

# Manifest key. Distinct from the Cloud Run service name so the entry
# stays stable if the service is ever renamed.
MANIFEST_KEY = "betting_control"

ENDPOINTS: dict[str, str] = {
    "admin_status": "/admin/status",
    "admin_config": "/admin/config",
    "admin_stats": "/admin/stats",
    "admin_activity": "/admin/activity",
    "admin_control": "/admin/control/{action}",
    "admin_events": "/admin/events",
}


def _disabled() -> bool:
    return bool(os.environ.get("BC_DISABLE_GCP_IO"))


def build_manifest_entry() -> dict[str, Any]:
    s = get_settings()
    return {
        "name": SERVICE_DESCRIPTION,
        "service": SERVICE_NAME,
        "type": "control",
        "url": s.service_url,
        "depends_on": ["fsu1b"],
        "endpoints": ENDPOINTS,
        "status": "active",
        "phase": 1,
        "last_registered": datetime.now(timezone.utc).isoformat(),
    }


def register() -> dict[str, Any]:
    """Read, merge our entry, write back. Raises on GCS failure."""
    entry = build_manifest_entry()
    if _disabled():
        logger.info("BC_DISABLE_GCP_IO set — returning entry without GCS write.")
        return entry

    s = get_settings()
    from google.cloud import storage  # type: ignore[import-not-found]

    client = storage.Client()
    blob = client.bucket(s.manifest_bucket).blob(s.manifest_blob)

    manifest: dict[str, Any] = {}
    if blob.exists():
        try:
            loaded = json.loads(blob.download_as_text())
            if isinstance(loaded, dict):
                manifest = loaded
            else:
                logger.warning(
                    "Manifest at gs://%s/%s is not an object — replacing.",
                    s.manifest_bucket, s.manifest_blob,
                )
        except json.JSONDecodeError as exc:
            # Do NOT clobber a manifest we merely failed to parse — other
            # services' entries live in this same blob.
            raise RuntimeError(f"manifest is not valid JSON: {exc}") from exc

    manifest[MANIFEST_KEY] = entry
    blob.upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )
    logger.info(
        "Registered '%s' in gs://%s/%s",
        MANIFEST_KEY, s.manifest_bucket, s.manifest_blob,
    )
    return entry


def register_best_effort() -> dict[str, Any] | None:
    """Try to register; swallow errors and return None on failure."""
    try:
        return register()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source manifest registration failed: %s", exc)
        return None
