"""Compatibility entry point for probabilistic MBAL OpenServer campaigns.

The maintained implementation and CLI live in :mod:`mbal_core` and ``mbal.py``.
This public-safe wrapper preserves the historical script/module name:

    python probabilistic_mbal_openserver.py --config mbal_config.local.yaml --dry-run

Use a gitignored local YAML for model paths, object names, tags, and priors.
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
