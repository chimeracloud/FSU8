"""
Standard observability endpoints — all five, identical across every FSU.

  /health   liveness
  /ready    readiness — dependencies reachable
  /info     static metadata
  /metrics  Prometheus text format
  /status   rich human-readable summary

ADAPTED from FSU1B. Two differences, both required by the shell
specification rather than by this service:

  * `/metrics` adds CPU, memory, requests, errors and latency. FSU1B's
    version reports uptime and stream counters only. CPU and RSS come
    from the stdlib (`resource`, `time.process_time`) rather than
    psutil — a dependency the shell does not otherwise need, for
    numbers Cloud Monitoring already collects at container level.
  * `/info` adds `build_sha` and `dependencies`.

`/ready` reports dependency reachability WITHOUT calling out. A
readiness probe that makes a network call to FSU1B on every poll would
put Cloud Run's health checks onto the critical path of another
service, and a slow gateway would then take this one down too. It
reports whether the dependency is *configured*; whether it *answers* is
what the bet path reports, at the moment it matters.
"""
from __future__ import annotations

import os
import resource
import sys
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Response

from core.config import get_settings
from core.events import app_state
from core.version import PHASE, SERVICE_DESCRIPTION, SERVICE_NAME, VERSION
from models.schemas import (
    HealthResponse,
    InfoResponse,
    ReadyResponse,
    StatusResponse,
)

router = APIRouter(tags=["observability"])

_START = time.time()

# Cloud Build stamps this; empty when running locally.
BUILD_SHA = os.environ.get("BUILD_SHA", "")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness — the process is up. Never touches a dependency."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
def ready(response: Response) -> ReadyResponse:
    """Readiness — the service is configured well enough to do its job.

    Phase 1 has no bet path, so the only dependency that matters is the
    FSU1B URL. Unset means a bet could not be placed, which is a real
    not-ready condition and is reported as 503 rather than papered over.
    """
    s = get_settings()
    gateway_configured = bool(s.fsu1b_url)

    deps = {
        "fsu1b": {
            "configured": gateway_configured,
            "url": s.fsu1b_url or None,
            "timeout_s": s.fsu1b_timeout_s,
        }
    }

    if not gateway_configured:
        response.status_code = 503
        return ReadyResponse(
            ready=False,
            phase=PHASE,
            service_state=app_state.service_state,
            dependencies=deps,
            note="fsu1b_url is not set — PUT /admin/config to configure it",
        )

    return ReadyResponse(
        ready=True,
        phase=PHASE,
        service_state=app_state.service_state,
        dependencies=deps,
    )


@router.get("/info", response_model=InfoResponse)
def info() -> InfoResponse:
    s = get_settings()
    return InfoResponse(
        service=SERVICE_NAME,
        version=VERSION,
        phase=PHASE,
        description=SERVICE_DESCRIPTION,
        region=s.region,
        project=s.gcp_project,
        build_sha=BUILD_SHA,
        dependencies=["fsu1b"],
        stream=None,
    )


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus text exposition format."""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is KILOBYTES on Linux and BYTES on macOS/BSD. Scaling
    # unconditionally reported 73GB on a laptop; the container runs
    # Linux, so the wrong branch would have been invisible in
    # production and wrong everywhere else.
    rss_bytes = ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024

    lines = [
        "# HELP fsu8_uptime_seconds Seconds since the service started.",
        "# TYPE fsu8_uptime_seconds counter",
        f"fsu8_uptime_seconds {time.time() - _START:.3f}",
        "# HELP fsu8_cpu_seconds_total Process CPU time consumed.",
        "# TYPE fsu8_cpu_seconds_total counter",
        f"fsu8_cpu_seconds_total {ru.ru_utime + ru.ru_stime:.3f}",
        "# HELP fsu8_memory_rss_bytes Peak resident set size.",
        "# TYPE fsu8_memory_rss_bytes gauge",
        f"fsu8_memory_rss_bytes {rss_bytes}",
        "# HELP fsu8_requests_total HTTP requests handled.",
        "# TYPE fsu8_requests_total counter",
        f"fsu8_requests_total {app_state.request_count}",
        "# HELP fsu8_errors_total HTTP responses with status >= 500.",
        "# TYPE fsu8_errors_total counter",
        f"fsu8_errors_total {app_state.error_count}",
        "# HELP fsu8_requests_per_second Recent request rate (60s window).",
        "# TYPE fsu8_requests_per_second gauge",
        f"fsu8_requests_per_second {app_state.requests_per_s_recent():.3f}",
    ]

    p50 = app_state.latency_ms_p50()
    if p50 is not None:
        lines += [
            "# HELP fsu8_request_latency_ms_p50 Median request latency.",
            "# TYPE fsu8_request_latency_ms_p50 gauge",
            f"fsu8_request_latency_ms_p50 {p50:.3f}",
        ]
    mx = app_state.latency_ms_max()
    if mx is not None:
        lines += [
            "# HELP fsu8_request_latency_ms_max Slowest request observed.",
            "# TYPE fsu8_request_latency_ms_max gauge",
            f"fsu8_request_latency_ms_max {mx:.3f}",
        ]

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(
        service=SERVICE_NAME,
        version=VERSION,
        phase=PHASE,
        service_state=app_state.service_state,
        uptime_s=round(app_state.uptime_s(), 3),
        request_count=app_state.request_count,
        error_count=app_state.error_count,
        now=datetime.now(timezone.utc),
    )
