# Proposed shell change — structured logging fields

**Raised by:** FSU8 (Live Betting Control), during Phase 1
**Date:** 2026-09-04
**Policy:** CHI-POL-008 §5 — Rule of Global Adjustment
**Status:** PROPOSAL. **Nothing has been changed outside FSU8.**

---

## The requirement, and the shortfall

The standard FSU shell requires every structured log entry to carry
**`service_name`, `trace_id` and `timestamp`**.

Building FSU8's shell from FSU1B's revealed that FSU1B's
`core/logging.py` emits none of the three. Under §5 a shell shortfall
is corrected in the shell and rolled to **all** FSUs, never patched
locally in the one that found it — so this is a platform defect, not an
FSU8 problem, and FSU8's fix should not be allowed to stand alone.

## Who is affected

Surveyed 2026-09-04 by reading each repo's logging module on its
deployed branch.

| Repo @ branch | Module | `service_name` | `trace_id` | `timestamp` |
|---|---|---|---|---|
| `FSU8` @ main | `core/logging.py` | **yes** | **yes** | **yes** |
| `FSU1B` @ main | `core/logging.py` | no | no | no |
| `fsu100` @ track1-production | `core/logging.py` | yes | **no** | yes |
| `fsu100` @ main | `core/logging.py` | yes | **no** | yes |
| `fsu100v2` @ main | `core/logging.py` | no | no | no |
| `fsu1a` @ main | *none found* | — | — | — |
| `fsu1e` @ main | *none found* | — | — | — |
| `cst-api` @ main | *none found* | — | — | — |

**No FSU other than FSU8 is compliant.** `trace_id` is absent
everywhere. Three services appear to have no structured logging module
at all, so they are emitting whatever `logging.basicConfig` or bare
uvicorn produces — those are a larger piece of work than a field
addition and should be scoped separately.

FSU1B and fsu100v2 emit `ts`, not `timestamp`. That is a rename, and a
rename breaks any log-based metric or alert filtering on the old name.

## The reference implementation

`FSU8/core/logging.py`. Emits:

```json
{
  "timestamp":    "2026-09-04T16:15:57.622217+00:00",
  "severity":     "INFO",
  "level":        "INFO",
  "service_name": "fsu8-betting-control",
  "trace_id":     "abcdef1234567890",
  "logger":       "core.config",
  "message":      "Config loaded from gs://..."
}
```

Two details worth carrying over rather than reinventing:

**`severity` as well as `level`.** Cloud Logging reads `severity` to
colour and filter entries; `level` is kept for anyone grepping raw
stdout. FSU1B emits only `level`, so its entries are all severity
`DEFAULT` in Cloud Logging today — which is why an ERROR there does not
stand out in the console.

**`trace_id` from a contextvar, seeded from Cloud Run's header.** A
middleware reads `X-Cloud-Trace-Context`, takes the portion before the
`/`, and sets a `ContextVar`. Outside a request it reads `"-"`. Seeding
from the platform header rather than generating a fresh id means a log
line joins to the trace Google already recorded, so a slow request can
be followed across services instead of only within one.

That middleware is the only part that is not a drop-in: it must be
added to each service's `main.py`, and a service without it still gets
`service_name` and `timestamp` with `trace_id` as `"-"` — a partial but
safe adoption.

## Risk

The risk is not in the code — it is roughly 40 lines and has no runtime
dependency. It is in **changing logging on services that are trading.**

| Service | Risk | Note |
|---|---|---|
| `fsu100-track1` | **Highest** | Trades real money. Deploying it needs a full image rebuild and a manual START + GO LIVE afterwards, and its own incident runbook says do not deploy during racing |
| `fsu1b` | Low now | Idle, holds no session. **Change it while it is stopped** |
| `fsu100v2`, `fsu1a`, `fsu1e` | Low | Not on the trading path |
| `cst-api` | Low | Portal API |

**A renamed field breaks anything filtering on the old one.** Before
rolling this, check whether any Cloud Monitoring log-based metric or
alert filters on `ts`. The `AUTH_FAILED` alert policy in
`fsu100/track1/ops/alert_auth_failed.json` is the known one and should
be re-read before `fsu100` is touched.

## Suggested sequence

1. **`FSU1B` first.** It is idle, it is where the shortfall was found,
   and it is the shell other FSUs are copied from. Fixing it first
   stops the defect propagating into the next service built from it.
2. `fsu100v2`, then `fsu1e`, then `fsu1a` — none on the trading path.
3. **`fsu100` / `fsu100-track1` last**, outside racing hours, as its
   own change with its own verification. Never bundled with anything
   else.
4. `cst-api` whenever convenient.

Steps 1 and 2 are safe to schedule now. Step 3 is Charles's call on
timing.

## What is NOT proposed

- No logging library, no new dependency, no log shipping change.
- No change to what is logged — only the envelope each entry carries.
- **No change to any service in this proposal.** FSU8 is compliant
  because it was written that way today; every other service is
  untouched and will stay untouched until Charles schedules it.
