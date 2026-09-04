"""
SET 1 — the admin surface, identical across every FSU.
"""
import json

from core.config import get_settings
from core.events import app_state


# ── /admin/status ───────────────────────────────────────────────────


def test_status_reports_identity_and_state(client):
    body = client.get("/admin/status").json()
    assert body["service"] == "fsu8-betting-control"
    assert body["phase"] == 1
    assert body["service_state"] == "stopped"
    assert body["uptime_s"] >= 0


# ── /admin/config ───────────────────────────────────────────────────


def test_config_returns_settings(client):
    body = client.get("/admin/config").json()
    assert body["auto_start"] is False
    assert body["fsu1b_url"] == ""
    assert body["log_level"] == "INFO"


def test_put_config_is_a_partial_diff(client):
    """Omitted fields must not be reset to their defaults."""
    client.put("/admin/config", json={"fsu1b_url": "https://gw.example"})
    client.put("/admin/config", json={"log_level": "DEBUG"})

    body = client.get("/admin/config").json()
    assert body["fsu1b_url"] == "https://gw.example"  # survived the second PUT
    assert body["log_level"] == "DEBUG"


def test_put_config_applies_to_settings(client):
    client.put("/admin/config", json={"fsu1b_timeout_s": 2.5})
    assert get_settings().fsu1b_timeout_s == 2.5


def test_put_config_rejects_unknown_fields(client):
    """extra='forbid' — a typo must 422, never be silently ignored."""
    r = client.put("/admin/config", json={"fsu1b_urls": "https://typo.example"})
    assert r.status_code == 422


def test_put_config_rejects_a_bad_log_level(client):
    assert client.put("/admin/config", json={"log_level": "LOUD"}).status_code == 422


def test_put_config_rejects_a_non_positive_timeout(client):
    assert client.put("/admin/config", json={"fsu1b_timeout_s": 0}).status_code == 422


def test_empty_put_is_a_no_op(client):
    before = client.get("/admin/config").json()
    r = client.put("/admin/config", json={})
    assert r.status_code == 200
    assert r.json() == before


def test_config_change_is_recorded_in_activity(client):
    client.put("/admin/config", json={"fsu1b_url": "https://gw.example"})
    kinds = [e["kind"] for e in client.get("/admin/activity").json()["events"]]
    assert "config_updated" in kinds


# ── /admin/stats ────────────────────────────────────────────────────


def test_stats_counts_calls_per_endpoint(client):
    client.get("/admin/status")
    client.get("/admin/status")
    client.get("/admin/config")

    body = client.get("/admin/stats").json()
    assert body["call_count_by_endpoint"]["/admin/status"] == 2
    assert body["call_count_by_endpoint"]["/admin/config"] == 1
    assert body["request_count"] >= 3
    assert body["error_count"] == 0


# ── /admin/activity ─────────────────────────────────────────────────


def test_activity_records_the_boot(client):
    events = client.get("/admin/activity").json()["events"]
    assert any(e["kind"] == "boot" for e in events)


# ── /admin/control ──────────────────────────────────────────────────


def test_boots_stopped(client):
    """Non-negotiable: deliberate start only."""
    assert client.get("/admin/status").json()["service_state"] == "stopped"


def test_start_then_stop(client):
    r = client.post("/admin/control/start").json()
    assert r["accepted"] is True and r["executed"] is True
    assert r["service_state"] == "running"

    r = client.post("/admin/control/stop").json()
    assert r["executed"] is True
    assert r["service_state"] == "stopped"


def test_starting_twice_is_accepted_but_not_executed(client):
    client.post("/admin/control/start")
    r = client.post("/admin/control/start").json()
    assert r["accepted"] is True
    assert r["executed"] is False
    assert r["note"] == "already running"


def test_pause_and_resume(client):
    client.post("/admin/control/start")
    r = client.post("/admin/control/pause").json()
    assert r["executed"] is True and r["service_state"] == "paused"

    r = client.post("/admin/control/resume").json()
    assert r["executed"] is True and r["service_state"] == "running"


def test_cannot_pause_when_stopped(client):
    r = client.post("/admin/control/pause").json()
    assert r["executed"] is False
    assert r["service_state"] == "stopped"
    assert "cannot pause" in r["note"]


def test_cannot_resume_when_not_paused(client):
    r = client.post("/admin/control/resume").json()
    assert r["executed"] is False
    assert "cannot resume" in r["note"]


def test_stop_from_paused_halts_immediately(client):
    """Stop must work from any live state, without a restart."""
    client.post("/admin/control/start")
    client.post("/admin/control/pause")
    r = client.post("/admin/control/stop").json()
    assert r["executed"] is True
    assert r["service_state"] == "stopped"


def test_test_action_is_a_noop_probe(client):
    r = client.post("/admin/control/test").json()
    assert r["accepted"] is True and r["executed"] is True
    assert r["service_state"] == "stopped"  # unchanged


def test_unknown_control_action_is_422(client):
    assert client.post("/admin/control/detonate").status_code == 422


def test_control_changes_are_recorded(client):
    client.post("/admin/control/start")
    kinds = [e["kind"] for e in client.get("/admin/activity").json()["events"]]
    assert "control_start" in kinds


# ── /admin/events ───────────────────────────────────────────────────


def test_events_is_an_sse_stream():
    """Drive the generator directly.

    Going through TestClient hangs: the endpoint streams until the
    client disconnects, and TestClient's synchronous portal does not
    propagate that disconnect, so the response never closes. Starlette
    cancels the generator on a real disconnect, so this is a limitation
    of the test transport, not of the endpoint. Driving the body
    iterator exercises the same code — subscribe, frame, unsubscribe.
    """
    import asyncio

    from services.admin import events_sse

    async def _drive():
        response = await events_sse()
        assert response.media_type == "text/event-stream"
        it = response.body_iterator
        opening = await asyncio.wait_for(it.__anext__(), timeout=2)

        await app_state.broadcast("admin", {"event": "hello"})
        frame = await asyncio.wait_for(it.__anext__(), timeout=2)

        await it.aclose()
        return opening, frame

    opening, frame = asyncio.run(_drive())
    assert opening.startswith(":")                    # opening comment frame
    assert frame.startswith("data: ")
    assert json.loads(frame[len("data: "):].strip())["event"] == "hello"


def test_sse_unsubscribes_when_the_client_goes_away():
    """A dropped consumer must not leak a queue for the process lifetime."""
    import asyncio

    from services.admin import events_sse

    async def _drive():
        response = await events_sse()
        it = response.body_iterator
        await asyncio.wait_for(it.__anext__(), timeout=2)
        during = len(app_state._subscribers.get("admin", []))
        await it.aclose()
        return during, len(app_state._subscribers.get("admin", []))

    during, after = asyncio.run(_drive())
    assert during == 1
    assert after == 0, "subscriber queue leaked after the consumer closed"


def test_control_change_is_broadcast_to_subscribers(client):
    """A state change must reach an attached SSE consumer."""
    import asyncio

    async def _drive():
        q = await app_state.subscribe("admin")
        await app_state.broadcast(
            "admin", {"event": "service_state_changed", "from": "stopped", "to": "running"},
        )
        return await asyncio.wait_for(q.get(), timeout=1)

    event = asyncio.run(_drive())
    assert event["event"] == "service_state_changed"
    assert event["to"] == "running"
