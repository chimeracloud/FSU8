"""
Structured JSON logging.

Per CHI-POL-008 §3.5 — the same shape across every Chimera FSU so Cloud
Logging queries are uniform.

ADAPTED from FSU1B, not copied. FSU1B's version emits only
`ts / level / logger / message`; the shell specification requires every
entry to carry `service_name`, `trace_id` and `timestamp`. This module
adds those three. **FSU1B needs the same change** — the shell is meant
to be identical across FSUs, so this belongs upstream rather than only
here (CHI-POL-008 §1.5).

`trace_id` comes from a contextvar set per request by the middleware in
`main.py`, seeded from Cloud Run's `X-Cloud-Trace-Context` header when
present so a log line can be joined to the trace Google already
records. Outside a request (startup, background tasks) it reads "-".

Severity maps onto Cloud Logging via `severity`, which is the field
Cloud Logging actually reads; `level` is kept alongside it for humans
grepping raw stdout.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from core.version import SERVICE_NAME

# Per-request trace id. "-" when there is no request in scope.
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def current_trace_id() -> str:
    return trace_id_var.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "level": record.levelname,
            "service_name": SERVICE_NAME,
            "trace_id": trace_id_var.get(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
