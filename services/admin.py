"""
SET 1 — PARAMETERS. Identical across every Chimera FSU (CHI-ADR-010).

  GET   /admin/status
  GET   /admin/config
  PUT   /admin/config
  GET   /admin/stats
  GET   /admin/activity
  POST  /admin/control/{action}
  GET   /admin/events

ADAPTED from FSU1B's `services/admin.py`. The control verbs are the
shell's five — start / stop / pause / resume / test. FSU1B's stream and
session verbs are gone; there is no stream here.

Phase 1: `start` and `stop` move `service_state` and nothing else,
because there is nothing yet to start. That is deliberate — the state
machine is real and observable now, so when the bet path lands in Phase
2 it has a gate to hang from rather than inventing one late.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.config import get_settings, save_config_to_gcs
from core.events import app_state
from core.logging import configure_logging
from core.version import PHASE, SERVICE_NAME, VERSION
from models.schemas import (
    ActivityEvent,
    AdminActivityResponse,
    AdminConfigResponse,
    AdminConfigUpdate,
    AdminStatsResponse,
    AdminStatusResponse,
    ControlAction,
    ControlActionResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
UTC = timezone.utc


@router.get("/status", response_model=AdminStatusResponse)
def status() -> AdminStatusResponse:
    return AdminStatusResponse(
        service=SERVICE_NAME,
        version=VERSION,
        phase=PHASE,
        service_state=app_state.service_state,
        uptime_s=round(app_state.uptime_s(), 3),
        started_at=app_state.started_at,
        now=datetime.now(UTC),
    )


@router.get("/config", response_model=AdminConfigResponse)
def get_config() -> AdminConfigResponse:
    s = get_settings()
    return AdminConfigResponse(
        auto_start=s.auto_start,
        fsu1b_url=s.fsu1b_url,
        fsu1b_timeout_s=s.fsu1b_timeout_s,
        log_level=s.log_level,
        events_topic=s.events_topic,
        config_bucket=s.config_bucket,
        config_blob=s.config_blob,
    )


@router.put("/config", response_model=AdminConfigResponse)
def put_config(update: AdminConfigUpdate) -> AdminConfigResponse:
    """Partial diff, persisted to GCS (CHI-POL-006).

    A persistence failure does NOT roll back the in-memory change: the
    operator retries the PUT to re-persist. The 502 says the change took
    effect but did not stick, which is more useful than pretending
    nothing happened when the running service has already changed.
    """
    changes = {
        k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None
    }
    if not changes:
        return get_config()

    persisted = save_config_to_gcs(changes)

    # Log level applies immediately — an operator raising it to DEBUG
    # during an incident should not have to wait for a restart.
    if "log_level" in changes:
        configure_logging(get_settings().log_level)

    app_state.add_activity("config_updated", ", ".join(sorted(changes)))

    if not persisted:
        raise HTTPException(
            status_code=502,
            detail={
                "ok": False,
                "applied_in_memory": True,
                "persisted_to_gcs": False,
                "note": "settings changed but did NOT persist to GCS — retry PUT",
            },
        )
    return get_config()


@router.get("/stats", response_model=AdminStatsResponse)
def stats() -> AdminStatsResponse:
    return AdminStatsResponse(
        uptime_s=round(app_state.uptime_s(), 3),
        request_count=app_state.request_count,
        error_count=app_state.error_count,
        requests_per_s_recent=round(app_state.requests_per_s_recent(), 3),
        latency_ms_p50=app_state.latency_ms_p50(),
        latency_ms_max=app_state.latency_ms_max(),
        call_count_by_endpoint=dict(app_state.call_count_by_endpoint),
        last_call_at_by_endpoint=dict(app_state.last_call_at_by_endpoint),
        subscribers_by_channel=app_state.subscriber_count(),
    )


@router.get("/activity", response_model=AdminActivityResponse)
def activity() -> AdminActivityResponse:
    return AdminActivityResponse(
        events=[ActivityEvent(**e) for e in app_state.recent_activity(limit=100)]
    )


@router.post("/control/{action}", response_model=ControlActionResponse)
async def control(action: ControlAction) -> ControlActionResponse:
    """start | stop | pause | resume | test.

    `executed` distinguishes "the verb was valid and changed something"
    from "the verb was valid and was already true". Both are accepted;
    only the first executed.
    """
    now = datetime.now(UTC)
    before = app_state.service_state
    note = ""
    executed = False

    if action == "start":
        if before == "running":
            note = "already running"
        else:
            app_state.service_state = "running"
            executed = True
            note = "started"

    elif action == "stop":
        # Stop must halt placement immediately, without a redeploy or a
        # restart. In Phase 1 there is nothing to halt; the state moves
        # so the contract is observable from the first deploy.
        if before == "stopped":
            note = "already stopped"
        else:
            app_state.service_state = "stopped"
            executed = True
            note = "stopped"

    elif action == "pause":
        if before != "running":
            note = f"cannot pause from {before}"
        else:
            app_state.service_state = "paused"
            executed = True
            note = "paused"

    elif action == "resume":
        if before != "paused":
            note = f"cannot resume from {before}"
        else:
            app_state.service_state = "running"
            executed = True
            note = "resumed"

    else:  # test
        note = "ok"
        executed = True

    if executed and action != "test":
        app_state.add_activity(f"control_{action}", f"{before} -> {app_state.service_state}")
        await app_state.broadcast(
            "admin",
            {
                "event": "service_state_changed",
                "ts": now.isoformat(),
                "from": before,
                "to": app_state.service_state,
            },
        )

    return ControlActionResponse(
        action=action,
        accepted=True,
        executed=executed,
        service_state=app_state.service_state,
        note=note,
        at=now,
    )


@router.get("/events")
async def events_sse() -> StreamingResponse:
    """SSE feed of control and lifecycle events for the portal."""

    async def _gen():
        q = await app_state.subscribe("admin")
        try:
            yield f": {SERVICE_NAME} admin event stream\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Comment frame keeps proxies and Cloud Run from
                    # reaping an idle connection.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await app_state.unsubscribe("admin", q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
