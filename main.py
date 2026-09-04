"""
Chimera Live Betting Control — FastAPI application.

    Engines  ->  LIVE BETTING CONTROL  ->  FSU1B  ->  Betfair

PHASE 1 — the standard FSU shell. Set 1 (admin) and the five
observability endpoints. **There is no bet placing in this build.** Set
3 arrives in Phase 2, per CHI-POL-008: shell first, deployed and
verified, then the content on top.

This service holds no Betfair credential and imports no exchange
library. It reaches Betfair only by calling FSU1B over HTTP with an IAM
ID token minted from its own Cloud Run identity. That is not a
preference — two services holding sessions on one Betfair app key
evicted each other mid-trade on 17 May, 31 July and 11 August 2026.

References:
  CHI-POL-003  Credentials in Secret Manager
  CHI-POL-004  --no-allow-unauthenticated
  CHI-POL-006  Portal as single auth boundary; settings in GCS
  CHI-POL-008  Shell-First Build Policy
  CHI-ADR-010  Three Endpoint Sets
  CHI-ADR-013  One task, one job
  Bible §20    Event envelope
  Bible §21    Source Manifest
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from core.config import get_settings, load_config_from_gcs, replace_settings
from core.events import app_state
from core.logging import configure_logging, trace_id_var
from core.version import SERVICE_DESCRIPTION, SERVICE_NAME, VERSION
from services import admin, observability
from services.event_publisher import publish
from services.source_manifest import register_best_effort

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    # Settings hydrate from GCS. The blob governs — the dataclass
    # default is decorative once a blob exists.
    load_config_from_gcs()

    # Deploy-time identity only. SERVICE_URL and GCP_PROJECT carry who
    # and where; never a tunable setting (CHI-POL-006).
    if os.environ.get("SERVICE_URL"):
        replace_settings(service_url=os.environ["SERVICE_URL"])
    if os.environ.get("GCP_PROJECT"):
        replace_settings(gcp_project=os.environ["GCP_PROJECT"])

    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings

    register_best_effort()

    await publish(
        "control_started",
        {
            "version": VERSION,
            "phase": 1,
            "auto_start": settings.auto_start,
            "service_url": settings.service_url,
        },
    )

    if settings.auto_start:
        # Honoured, but the default is False and the GCS blob is what
        # actually decides. Phase 1 has nothing to start beyond the
        # state flag itself.
        app_state.service_state = "running"
        app_state.add_activity("auto_start", "auto_start=true — booted running")
        logger.warning("auto_start=true — service booted RUNNING, not stopped.")
    else:
        app_state.add_activity("boot", "booted stopped — POST /admin/control/start")
        logger.info("auto_start=false — booted stopped.")

    try:
        yield
    finally:
        try:
            await publish("control_stopped", {"version": VERSION})
        except Exception:  # noqa: BLE001
            pass
        logger.info("%s shut down.", SERVICE_NAME)


app = FastAPI(
    title=SERVICE_NAME,
    description=SERVICE_DESCRIPTION,
    version=VERSION,
    docs_url="/admin/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# No CORS middleware, deliberately. The browser never talks to this
# service directly — the CST portal reaches it through the cst-api
# proxy (CHI-ADR-014). Adding CORS here would imply a browser origin
# that is not supposed to exist.


@app.middleware("http")
async def _observe(request: Request, call_next):
    """Stamp a trace id, time the request, count it.

    The trace id is seeded from Cloud Run's X-Cloud-Trace-Context so a
    log line can be joined to the trace Google already records.
    """
    header = request.headers.get("x-cloud-trace-context", "")
    trace = header.split("/")[0] if header else uuid.uuid4().hex
    token = trace_id_var.set(trace)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        try:
            app_state.note_request(request.url.path, elapsed_ms, status_code)
        except Exception:  # noqa: BLE001 — never break a request
            pass
        trace_id_var.reset(token)


app.include_router(observability.router)
app.include_router(admin.router)
