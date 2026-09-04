"""
Pydantic v2 models.

Phase 1 — the shell only. Set 1 (admin) and the observability
endpoints. The bet models arrive in Phase 2 and are deliberately absent
here: no placeholder, no stub, no commented-out shape.

`extra="forbid"` on every model, per the shell specification. On a
request model that is a safety property rather than a style choice: a
misspelled field in a bet request must be a 422, never a silently
ignored default.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ServiceStateLiteral = Literal["stopped", "running", "paused"]

ControlAction = Literal["start", "stop", "pause", "resume", "test"]


class Strict(BaseModel):
    """Base — every model in this service forbids unknown fields."""

    model_config = ConfigDict(extra="forbid")


# ── Set 1: /admin/status ────────────────────────────────────────────


class AdminStatusResponse(Strict):
    service: str
    version: str
    phase: int
    service_state: ServiceStateLiteral
    uptime_s: float
    started_at: datetime
    now: datetime


# ── Set 1: /admin/config ────────────────────────────────────────────


class AdminConfigResponse(Strict):
    auto_start: bool
    fsu1b_url: str
    fsu1b_timeout_s: float
    log_level: str
    events_topic: str
    config_bucket: str
    config_blob: str


class AdminConfigUpdate(Strict):
    """Partial diff — only the fields present are changed.

    Every field is optional and `None` means "not supplied". The PUT
    handler drops Nones before applying, so omitting a field never
    resets it to a default.
    """

    auto_start: bool | None = None
    fsu1b_url: str | None = None
    fsu1b_timeout_s: float | None = Field(default=None, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    events_topic: str | None = None
    config_bucket: str | None = None
    config_blob: str | None = None


# ── Set 1: /admin/stats ─────────────────────────────────────────────


class AdminStatsResponse(Strict):
    uptime_s: float
    request_count: int
    error_count: int
    requests_per_s_recent: float
    latency_ms_p50: float | None = None
    latency_ms_max: float | None = None
    call_count_by_endpoint: dict[str, int] = Field(default_factory=dict)
    last_call_at_by_endpoint: dict[str, datetime | None] = Field(default_factory=dict)
    subscribers_by_channel: dict[str, int] = Field(default_factory=dict)


# ── Set 1: /admin/activity ──────────────────────────────────────────


class ActivityEvent(Strict):
    ts: datetime
    kind: str
    detail: str


class AdminActivityResponse(Strict):
    events: list[ActivityEvent]


# ── Set 1: /admin/control/{action} ──────────────────────────────────


class ControlActionResponse(Strict):
    action: str
    accepted: bool
    executed: bool
    service_state: ServiceStateLiteral
    note: str
    at: datetime


# ── Observability ───────────────────────────────────────────────────


class HealthResponse(Strict):
    status: Literal["ok"]


class ReadyResponse(Strict):
    ready: bool
    phase: int
    service_state: ServiceStateLiteral
    dependencies: dict[str, Any]
    note: str | None = None


class InfoResponse(Strict):
    service: str
    version: str
    phase: int
    description: str
    region: str
    project: str
    build_sha: str
    dependencies: list[str]
    stream: str | None = None


class StatusResponse(Strict):
    service: str
    version: str
    phase: int
    service_state: ServiceStateLiteral
    uptime_s: float
    request_count: int
    error_count: int
    now: datetime
