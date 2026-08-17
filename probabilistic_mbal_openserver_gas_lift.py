"""Compatibility entry point for paired probabilistic MBAL gas-lift sweeps.

The maintained implementation is the unified ``mbal.py`` / :mod:`mbal_core`
CLI. This wrapper preserves the historical script/module name and accepts the
same ``--gas-lift-values`` option:

    python probabilistic_mbal_openserver_gas_lift.py \
        --config mbal_config.local.yaml --dry-run --gas-lift-values 0,0.5,1.0

Never place private model or object names in this committed wrapper.
"""

from __future__ import annotations

import mbal_core as _core

# Preserve the historical import surface without duplicating scientific logic.
globals().update({name: getattr(_core, name) for name in _core.__all__})


def main(argv: list[str] | None = None) -> int:
    return _core.main(argv, description=__doc__)


__all__ = [name for name in _core.__all__ if name != "main"] + ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
