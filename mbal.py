"""
Probabilistic MBAL via Petroleum Experts OpenServer
==================================================

One command. Each tank is sampled independently from its official STOIIP (the
P50) and optional P90/P10. Any MBAL input listed under controls: is written
before each prediction, and controls with a value list are swept.

    python mbal.py --config example.yaml --dry-run --n 200
    python mbal.py --config example.yaml --n 200
    python mbal.py --dry-run --n 200 --control gas_lift=0,0.5,1.0
    python mbal.py --summarize-only --out-dir mbal_output
    python mbal.py --write-example-config example.yaml

MBAL must already be open: OpenServer attaches to a running MBAL and cannot
start one. Implementation lives in mbal_core.py; volumes, controls and the
full workflow are in docs/use-guide.md.
"""

from __future__ import annotations

from mbal_core import main as _core_main


def main(argv: list[str] | None = None) -> int:
    return _core_main(argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
