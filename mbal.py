"""
Probabilistic MBAL via Petroleum Experts OpenServer
==================================================

One command. Always the same three tanks (working official 4.5 / 3.0 / 6.5
MSm3, connected-volume prior). Gas lift and water injection are optional
sweeps on that ensemble.

    python mbal.py --config example.yaml --dry-run --n 200
    python mbal.py --config example.yaml --n 200
    python mbal.py --dry-run --n 200 --gas-lift-values 0,0.5,1.0
    python mbal.py --dry-run --n 200 \\
        --water-inj-rate-values 0,300,600 --water-inj-bhp-values 250,300
    python mbal.py --summarize-only --out-dir mbal_output
    python mbal.py --write-example-config example.yaml

Implementation lives in mbal_core.py. How to run with MBAL open:
docs/use-guide.md. Why official is not the P50: docs/oil-in-place.md.
"""

from __future__ import annotations

from mbal_core import main as _core_main


def main(argv: list[str] | None = None) -> int:
    return _core_main(argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
