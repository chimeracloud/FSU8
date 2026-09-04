"""
The five standard observability endpoints.

Phase 1's acceptance criterion is that all five answer, so these are
the tests that decide whether the shell is done.
"""
import pytest

from core.config import replace_settings
from core.events import app_state


def test_health_is_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_never_touches_a_dependency(client):
    """Liveness must not fail because FSU1B is unset or unreachable."""
    replace_settings(fsu1b_url="")
    assert client.get("/health").status_code == 200


def test_ready_is_503_until_the_gateway_is_configured(client):
    replace_settings(fsu1b_url="")
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["dependencies"]["fsu1b"]["configured"] is False
    assert "fsu1b_url" in body["note"]


def test_ready_is_200_once_the_gateway_is_configured(client):
    replace_settings(fsu1b_url="https://fsu1b.example.run.app")
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["dependencies"]["fsu1b"]["url"] == "https://fsu1b.example.run.app"


def test_info_reports_identity_and_dependencies(client):
    body = client.get("/info").json()
    assert body["service"] == "fsu8-betting-control"
    assert body["phase"] == 1
    assert body["dependencies"] == ["fsu1b"]
    assert body["region"] == "europe-west2"
    assert body["project"] == "chiops"
    assert "build_sha" in body


def test_metrics_is_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # The shell spec names all five families explicitly.
    for metric in (
        "fsu8_cpu_seconds_total",
        "fsu8_memory_rss_bytes",
        "fsu8_requests_total",
        "fsu8_errors_total",
        "fsu8_uptime_seconds",
    ):
        assert metric in body, f"{metric} missing from /metrics"


def test_metrics_counts_requests(client):
    client.get("/health")
    client.get("/health")
    body = client.get("/metrics").text
    line = next(
        ln for ln in body.splitlines()
        if ln.startswith("fsu8_requests_total ")
    )
    assert int(float(line.split()[1])) >= 2


def test_status_is_a_human_summary(client):
    body = client.get("/status").json()
    assert body["service"] == "fsu8-betting-control"
    assert body["service_state"] == "stopped"
    assert body["uptime_s"] >= 0


@pytest.mark.parametrize(
    "path", ["/health", "/ready", "/info", "/metrics", "/status"],
)
def test_all_five_answer(client, path):
    """Phase 1 is not done unless every one of these responds."""
    assert client.get(path).status_code in (200, 503)


def test_observability_paths_stay_out_of_the_endpoint_feed(client):
    """Health pollers must not drown out real consumer traffic."""
    for _ in range(3):
        client.get("/health")
        client.get("/ready")
    client.get("/admin/status")

    counts = app_state.call_count_by_endpoint
    assert "/health" not in counts
    assert "/ready" not in counts
    assert counts.get("/admin/status") == 1


def test_ready_503_is_not_counted_as_a_server_error(client):
    """A designed 503 must not inflate the error metric.

    /ready returns 503 while fsu1b_url is unset. Counting that would
    make fsu8_errors_total climb on a healthy service and train the
    operator to ignore it.
    """
    replace_settings(fsu1b_url="")
    for _ in range(3):
        assert client.get("/ready").status_code == 503
    assert app_state.error_count == 0
    assert client.get("/admin/stats").json()["error_count"] == 0


def test_memory_rss_is_a_plausible_number(client):
    """ru_maxrss is KB on Linux, bytes on macOS — the wrong branch
    reported 73GB on a laptop and would have been invisible in prod."""
    line = next(
        ln for ln in client.get("/metrics").text.splitlines()
        if ln.startswith("fsu8_memory_rss_bytes ")
    )
    rss = float(line.split()[1])
    # A Python web process: comfortably over 8MB, nowhere near 8GB.
    assert 8 * 1024**2 < rss < 8 * 1024**3, f"implausible RSS: {rss}"
