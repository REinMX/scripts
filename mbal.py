"""
Probabilistic MBAL via Petroleum Experts OpenServer
==================================================

One command. Each tank is sampled independently around its official STOIIP,
which is both the mean and the P50. Gas lift and water injection are optional
sweeps on that ensemble.

    python mbal.py --config example.yaml --dry-run --n 200
    python mbal.py --config example.yaml --n 200
    python mbal.py --dry-run --n 200 --gas-lift-values 0,0.5,1.0
    python mbal.py --dry-run --n 200 \\
        --water-inj-rate-values 0,300,600 --water-inj-bhp-values 250,300
    python mbal.py --summarize-only --out-dir mbal_output
    python mbal.py --write-example-config example.yaml

MBAL must already be open: OpenServer attaches to a running MBAL and cannot
start one. Implementation lives in mbal_core.py. How to run:
docs/use-guide.md. Volume prior: docs/oil-in-place.md.
"""

from __future__ import annotations

from mbal_core import main as _core_main


def main(argv: list[str] | None = None) -> int:
    return _core_main(argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
