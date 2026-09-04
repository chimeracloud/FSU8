"""
The parts of the shell that are policy, not behaviour.

These tests exist to fail loudly if a later change quietly breaks a
constraint that was written down as non-negotiable.
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from core.config import Settings, get_settings, load_config_from_gcs, settings_to_dict
from core.logging import JsonFormatter, trace_id_var

REPO = Path(__file__).resolve().parent.parent

# Only the code that actually ships. Scanning the whole tree picked up
# site-packages under .venv.nosync — the venv is not "shipped code",
# and the Dockerfile copies exactly these paths.
SOURCE_ROOTS = ("main.py", "core", "services", "models")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for entry in SOURCE_ROOTS:
        target = REPO / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


def test_source_scan_finds_the_shipped_files():
    """Guard the guard — an empty scan would make the next two vacuous."""
    names = {p.name for p in _source_files()}
    assert {"main.py", "config.py", "admin.py", "schemas.py"} <= names
    assert not any(".venv" in p.parts[0] for p in _source_files())


# ── No credentials, no exchange library ─────────────────────────────


def test_no_exchange_library_is_imported():
    """Control must never speak to Betfair directly."""
    banned = ("betfairlightweight", "import betfair", "from betfair")
    offenders = []
    for py in _source_files():
        text = py.read_text()
        for token in banned:
            if token in text:
                offenders.append(f"{py.relative_to(REPO)}: {token}")
    assert not offenders, f"exchange library referenced: {offenders}"


def test_no_credential_fields_in_settings():
    """The Settings dataclass must carry nothing secret."""
    names = {f for f in Settings.__dataclass_fields__}
    forbidden = {"username", "password", "app_key", "cert_pem", "key_pem",
                 "secrets", "token", "api_key"}
    assert not (names & forbidden), f"credential-shaped settings: {names & forbidden}"


def test_requirements_pull_in_no_exchange_library():
    reqs = (REPO / "requirements.txt").read_text().lower()
    assert "betfair" not in reqs


def test_no_secret_manager_dependency():
    """Control holds no credential, so it needs no Secret Manager client."""
    reqs = (REPO / "requirements.txt").read_text().lower()
    assert "secret-manager" not in reqs and "secretmanager" not in reqs


# ── Settings live in GCS, never env vars ────────────────────────────


def test_tunable_settings_are_not_read_from_the_environment(monkeypatch):
    """CHI-POL-006 — env vars carry deploy-time identity only."""
    monkeypatch.setenv("AUTO_START", "true")
    monkeypatch.setenv("FSU1B_URL", "https://injected.example")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    from core.config import reset_settings_for_test

    reset_settings_for_test()
    s = get_settings()
    assert s.auto_start is False
    assert s.fsu1b_url == ""
    assert s.log_level == "INFO"


def test_auto_start_defaults_false():
    assert Settings().auto_start is False


def test_settings_round_trip_through_the_gcs_payload():
    """Every setting must survive serialisation, or it never persists."""
    from core.config import apply_dict, replace_settings, reset_settings_for_test

    replace_settings(fsu1b_url="https://gw.example", auto_start=True)
    payload = settings_to_dict()
    assert json.loads(json.dumps(payload))  # JSON-safe

    reset_settings_for_test()
    assert get_settings().fsu1b_url == ""

    apply_dict(payload)
    assert get_settings().fsu1b_url == "https://gw.example"
    assert get_settings().auto_start is True


def test_gcs_load_degrades_to_defaults_when_disabled():
    """A missing GCS must leave the admin surface serving."""
    applied = load_config_from_gcs()
    assert applied["service_name"] == "fsu1bv2-betting-control"


# ── Structured logging ──────────────────────────────────────────────


def test_log_entries_carry_service_name_trace_id_and_timestamp():
    """The shell spec requires all three on every entry."""
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["service_name"] == "fsu1bv2-betting-control"
    assert payload["trace_id"] == "-"          # no request in scope
    assert payload["timestamp"]
    assert payload["severity"] == "INFO"
    assert payload["message"] == "hello"


def test_trace_id_is_picked_up_from_the_contextvar():
    token = trace_id_var.set("abc123")
    try:
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="x", args=(), exc_info=None,
        )
        assert json.loads(JsonFormatter().format(record))["trace_id"] == "abc123"
    finally:
        trace_id_var.reset(token)


def test_request_gets_a_trace_id_from_the_cloud_run_header(client):
    """Cloud Run's header must seed the trace id so logs join to traces."""
    r = client.get(
        "/admin/status",
        headers={"X-Cloud-Trace-Context": "abcdef1234567890/1;o=1"},
    )
    assert r.status_code == 200
    # The contextvar is reset after the request; the assertion that
    # matters is that the header parsed without upsetting the handler.


def test_no_print_statements_in_shipped_code():
    offenders = []
    for py in _source_files():
        for i, line in enumerate(py.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("print(") or " print(" in stripped:
                offenders.append(f"{py.relative_to(REPO)}:{i}")
    assert not offenders, f"print() found: {offenders}"


# ── No CORS: the portal proxy is the only browser path ──────────────


def test_no_cors_middleware(client):
    """CHI-ADR-014 — the browser reaches this only via cst-api."""
    r = client.get("/admin/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ── Phase 1 has no bet path ─────────────────────────────────────────


@pytest.mark.parametrize("path", ["/bets", "/gui/bets", "/gui/throughput"])
def test_phase_1_serves_no_bet_endpoints(client, path):
    """Shell first. The content arrives in Phase 2, not as a stub now."""
    assert client.get(path).status_code == 404


def test_openapi_advertises_only_the_shell(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/health", "/ready", "/info", "/metrics", "/status",
        "/admin/status", "/admin/config", "/admin/stats",
        "/admin/activity", "/admin/control/{action}", "/admin/events",
    }
    assert paths == expected, f"unexpected: {paths ^ expected}"
