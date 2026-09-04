"""Service identity. Single source of truth used in `/info` and `/status`.

Naming note: the build brief's convention is `fsu[n]` / `fsu[n]-[function]`
with the number to be allocated. Charles named the repo `FSU1Bv2` on
2026-09-04, so that is what is used here. The Cloud Run service follows
the brief's `[repo]-[function]` shape — confirm before the first deploy.
"""

SERVICE_NAME: str = "fsu1bv2-betting-control"
SERVICE_DESCRIPTION: str = "Chimera Live Betting Control"
VERSION: str = "0.1.0-phase1"
PHASE: int = 1  # Per CHI-POL-008 phasing. Phase 1 = shell only.
