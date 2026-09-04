# Chimera Live Betting Control

> Born from complexity. Engineered for certainty.

One job: **accept a bet request, place it, return and monitor the
outcome.**

```
Engines  ->  LIVE BETTING CONTROL  ->  FSU1B  ->  Betfair
```

The engine sends a bet request. Control places it through FSU1B and
returns what happened. Nothing else.

## What this service is not

No deduplication. No risk checks. No authorisation logic. No audit
subsystem. No recorders. No retry logic. No exposure tracking. No venue
router abstraction. No statistics. No DRY_RUN mode.

Those are **absent, not deferred**. There are no stubs, no hooks and no
`# TODO` markers holding a place for them. If one is needed later it
gets built then, with the reasoning visible in that commit rather than
guessed at now.

## It holds no credential

**The engine holds no Betfair credential and never calls Betfair.
Control holds no Betfair credential and never calls Betfair.** FSU1B
holds the session; Control calls FSU1B over HTTP with an IAM ID token
minted from its own Cloud Run identity.

This is a structural response to a real failure, not a preference. Two
services holding sessions on one Betfair app key evicted each other
mid-session, with open positions, on **17 May, 31 July and 11 August
2026**. `tests/test_shell_contract.py` fails the build if an exchange
library is imported or a credential-shaped setting is added.

## Status — PHASE 1: SHELL ONLY

Per CHI-POL-008 (Shell-First Build Policy), the shell is built,
deployed and verified **before** any bet placing is added. The policy
exists because two services built content-first in May 2026 drifted
apart within days.

| Phase | Status | What |
|---|---|---|
| 1 — Shell | **this build** | Set 1 admin + five observability endpoints. No bet path |
| 2 — Content | not started | `POST /bets`, cancel, replace, outcome monitoring, Set 2 GUI |

**There is no bet endpoint in this build.** `GET /bets` returns 404,
and a test asserts it.

## Endpoints

### Set 1 — PARAMETERS (identical across every Chimera FSU)

| | |
|---|---|
| `GET /admin/status` | identity, service state, uptime |
| `GET /admin/config` | current settings |
| `PUT /admin/config` | partial diff; persists to GCS; **502 if the write fails** |
| `GET /admin/stats` | requests, errors, latency, per-endpoint counts |
| `GET /admin/activity` | last 100 events |
| `POST /admin/control/{action}` | `start` \| `stop` \| `pause` \| `resume` \| `test` |
| `GET /admin/events` | SSE |

### Standard observability

| | |
|---|---|
| `GET /health` | liveness — never touches a dependency |
| `GET /ready` | readiness — **503 while `fsu1b_url` is unset** |
| `GET /info` | name, version, phase, region, project, build sha, dependencies |
| `GET /metrics` | Prometheus: CPU, memory, requests, errors, latency, uptime |
| `GET /status` | human-readable summary |

`/ready` reports whether the gateway is *configured*, and deliberately
does not call it. A readiness probe that makes a network call to FSU1B
on every poll would put Cloud Run's health checks on another service's
critical path, so a slow gateway would take this one down too. Whether
FSU1B *answers* is reported by the bet path, at the moment it matters.

## Configuration

Settings live in `gs://chiops-betfair-recording/config/betting_control.json`
and are edited through `PUT /admin/config`. **Never environment
variables** (CHI-POL-006) — env vars carry deploy-time identity only
(`SERVICE_URL`, `GCP_PROJECT`, `BUILD_SHA`).

**The blob governs. The dataclass default is decorative.** FSU1B was
found running with `auto_start: true` in GCS overriding a `False` code
default, having silently opened a live Betfair session on boot. A test
asserts that no tunable setting is readable from the environment.

| Setting | Default | Meaning |
|---|---|---|
| `auto_start` | `false` | **Non-negotiable.** Boots stopped; started deliberately |
| `fsu1b_url` | `""` | The gateway. `/ready` is 503 until set |
| `fsu1b_timeout_s` | `10.0` | Upstream call timeout |
| `log_level` | `INFO` | Applied immediately on PUT |
| `events_topic` | `chimera-events` | See the note below |

## Local development

```bash
python3.13 -m venv .venv.nosync
source .venv.nosync/bin/activate
pip install -r requirements-dev.txt

export BC_DISABLE_GCP_IO=1     # short-circuits GCS + Pub/Sub
uvicorn main:app --port 8080
pytest -q
```

`BC_DISABLE_GCP_IO` is set automatically by `tests/conftest.py`, before
any project module is imported. Without it the suite would write to the
production config blob and the shared Source Manifest.

## Deployment

Push to `main` -> Cloud Build -> Cloud Run, driven by `cloudbuild.yaml`.

> **Create the trigger with `--build-config=cloudbuild.yaml`. Do not use
> the Cloud Run "continuously deploy" wizard.**
>
> The wizard writes its own inline build whose deploy step is
> `gcloud run services update --image --labels` — image and labels
> only. FSU1B ran for three months on `min-instances=0`,
> `max-instances=20` and CPU throttling while its `cloudbuild.yaml`
> specified `1 / 1 / no-throttling`, because nothing ever read that
> file and the build went green either way.

| Flag | Why |
|---|---|
| `--min-instances=1` | A bet request must not pay a cold start |
| `--max-instances=1` | Bet state is in-process; a second instance would answer `GET /bets` from a different, incomplete view |
| `--no-cpu-throttling` | Phase 2's outcome polling runs between requests |
| `--no-allow-unauthenticated` | CHI-POL-004. Browser access via the cst-api proxy only |

There is no CORS middleware, deliberately — adding it would imply a
browser origin that is not supposed to exist.

## Open items requiring Charles

1. **Cloud Run service name.** `fsu1bv2-betting-control` follows the
   brief's `[repo]-[function]` convention. Confirm or shorten.
2. **Service account `fsu1bv2-sa@chiops`** — needs `roles/run.invoker`
   on `fsu1b` **only**, plus object read/write on the config bucket and
   `roles/pubsub.publisher` on the events topic. Not created.
3. **Pub/Sub topic `chimera-events` does not exist.** The convention in
   chiops is per-service (`chimera-fsu1b-events`,
   `chimera-fsu100v2-events`). Either create the shared topic or point
   `events_topic` at a per-service one. Until then the publisher's stub
   fallback logs envelopes to stdout and the admin SSE channel, so the
   service runs either way.

## What the history taught this design

**Session collisions (17 May, 31 July, 11 August 2026).** Two services
on one app key evicting each other mid-session. The 11 August incident
report records the engine authenticating once per process lifetime with
no keep-alive, then failing for 1,000+ requests overnight into a
trading day. *Control therefore holds no credential at all* — there is
no session for it to collide with.

**Pre- versus post-modifier stake.** The engine computes
`final_stake = base_stake * mark_uplift * point_value`, displayed one
and executed the other, and two false anomaly investigations followed.
*Control will record the stake the engine sent and the stake that was
placed as two separate fields, and never assume they are the same
number* — even though, holding no modifier logic, it expects them to
match. If they ever diverge, that is a finding, not a rounding error.

## References

- CHI-POL-003 — Credentials in Secret Manager
- CHI-POL-004 — `--no-allow-unauthenticated`
- CHI-POL-006 — Portal as single auth boundary; settings in GCS
- CHI-POL-008 — Shell-First Build Policy
- CHI-ADR-010 — Three Endpoint Sets
- CHI-ADR-013 — One task, one job
- CHI-ADR-014 — Portal Proxy Pattern
- Bible §20 — Event Envelope
- Bible §21 — Source Manifest
