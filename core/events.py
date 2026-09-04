"""
Service state, activity feed and SSE pub/sub.

ADAPTED from FSU1B's `core/state.py`, which bundles the same three
concerns. Everything stream-specific is gone — there is no stream here,
no market cache, no session. What is left is the shell's own state:

  * `service_state`  — stopped | running | paused, driven by
                       POST /admin/control/{start|stop|pause|resume}
  * activity ring    — the last 200 events, for GET /admin/activity
  * request counters — for GET /admin/stats and GET /metrics
  * SSE pub/sub      — for GET /admin/events

`service_state` boots at "stopped" and only an operator moves it
(CHI-POL: deliberate start). Phase 1 has nothing to start, so the state
is real but nothing is gated on it yet; the gate arrives with the bet
placing in Phase 2.
"""
from __future__ import annotations

import asyncio
import time as _t
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Optional

UTC = timezone.utc

ServiceState = Literal["stopped", "running", "paused"]

# Observability paths are excluded from the per-endpoint feed so that
# health pollers don't drown out real consumer traffic.
OBSERVABILITY_PATHS = frozenset(
    {"/health", "/ready", "/info", "/metrics", "/status"}
)


class AppState:
    """Singleton, accessed via the module-level ``app_state``."""

    def __init__(self) -> None:
        self.started_at: datetime = datetime.now(UTC)
        self.service_state: ServiceState = "stopped"

        # Request counters -> /admin/stats and /metrics.
        self.request_count: int = 0
        self.error_count: int = 0
        self.last_call_at_by_endpoint: dict[str, datetime] = {}
        self.call_count_by_endpoint: dict[str, int] = {}
        self._latencies_ms: deque[float] = deque(maxlen=4096)
        self._request_ts: deque[float] = deque(maxlen=4096)

        self._activity: deque[dict] = deque(maxlen=200)

        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._sub_lock = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self.service_state == "running"

    def uptime_s(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    # ── Activity feed ───────────────────────────────────────────────

    def add_activity(self, kind: str, detail: str) -> None:
        self._activity.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": kind,
                "detail": detail,
            }
        )

    def recent_activity(self, limit: int = 100) -> list[dict]:
        return list(self._activity)[-limit:]

    # ── Request accounting ──────────────────────────────────────────

    def note_request(self, path: str, latency_ms: float, status_code: int) -> None:
        self.request_count += 1
        # /ready returns 503 BY DESIGN while a dependency is
        # unconfigured. Counting that as a server error makes
        # bc_errors_total climb on a perfectly healthy service and
        # trains the operator to ignore the metric. Every other path's
        # 5xx is a real error and is counted.
        if status_code >= 500 and path != "/ready":
            self.error_count += 1
        self._latencies_ms.append(latency_ms)
        self._request_ts.append(_t.time())
        if path not in OBSERVABILITY_PATHS:
            self.last_call_at_by_endpoint[path] = datetime.now(UTC)
            self.call_count_by_endpoint[path] = (
                self.call_count_by_endpoint.get(path, 0) + 1
            )

    def requests_per_s_recent(self, window_s: float = 60.0) -> float:
        now = _t.time()
        recent = sum(1 for ts in self._request_ts if (now - ts) <= window_s)
        return recent / window_s

    def latency_ms_p50(self) -> Optional[float]:
        if not self._latencies_ms:
            return None
        ordered = sorted(self._latencies_ms)
        return ordered[len(ordered) // 2]

    def latency_ms_max(self) -> Optional[float]:
        return max(self._latencies_ms) if self._latencies_ms else None

    # ── SSE pub/sub ─────────────────────────────────────────────────

    async def subscribe(self, channel: str = "admin") -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._sub_lock:
            self._subscribers.setdefault(channel, []).append(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        async with self._sub_lock:
            subs = self._subscribers.get(channel, [])
            self._subscribers[channel] = [s for s in subs if s is not q]

    async def broadcast(self, channel: str, event: dict) -> None:
        """Publish to a channel; mirrors to 'all'."""
        async with self._sub_lock:
            for ch in {channel, "all"}:
                for q in self._subscribers.get(ch, []):
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        # Drop rather than block — a slow consumer must
                        # never stall the producer.
                        pass

    def subscriber_count(self) -> dict[str, int]:
        return {ch: len(subs) for ch, subs in self._subscribers.items()}


app_state = AppState()


def reset_state_for_test() -> None:
    """Test-only — reset the singleton's fields in place.

    Must mutate the existing object rather than rebind the module-level
    name: other modules import `app_state` by reference, so rebinding
    would leave them holding the old instance and stale flags would leak
    between tests.
    """
    fresh = AppState()
    app_state.__dict__.update(fresh.__dict__)
