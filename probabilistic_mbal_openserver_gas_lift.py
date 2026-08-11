"""
Probabilistic MBAL + gas-lift sensitivity via OpenServer
========================================================

Same independent per-tank STOIIP sampling as the base script, plus:

- name-based tank input tags (MBAL V16.5 style)
- optional per-tank AQUIFVOLUME distributions
- deterministic gas-lift rate sweep paired across every geological realization

Usage
-----
    python probabilistic_mbal_openserver_gas_lift.py --dry-run --n 200 \\
        --gas-lift-values 0,0.5,1.0,1.5
    python probabilistic_mbal_openserver_gas_lift.py --config gas_lift_config.yaml
    python probabilistic_mbal_openserver_gas_lift.py --summarize-only
    python probabilistic_mbal_openserver_gas_lift.py \\
        --write-example-config example_gas_lift_config.yaml

Before a licensed run, replace REPLACE_WITH_* tank/well names (or set them in YAML)
with exact object names from MBAL's OpenServer browser.

Implementation lives in mbal_core.py.
"""

from __future__ import annotations

from mbal_core import (  # noqa: F401
    DEFAULT_INDEX_TAGS,
    DEFAULT_NAME_TAGS,
    Config,
    Distribution,
    OpenServer,
    SetServer,
    TankConfig,
    apply_realization,
    build_sample_table,
    config_from_dict,
    config_to_dict,
    default_config,
    load_config_yaml,
    lognormal_from_p90_p10,
    new_result_record,
    percentiles,
    read_results,
    run_monte_carlo,
    sample_distribution,
    setup_logging,
    summarize,
    unit_hypercube,
    validate_config,
    validate_distribution,
    validate_openserver_tags,
)
from mbal_core import main as _core_main

# Module-level defaults match the gas-lift / name-based workflow.
CFG = default_config(gas_lift=True)
TAGS = dict(DEFAULT_NAME_TAGS)


def main(argv: list[str] | None = None) -> int:
    return _core_main(
        argv,
        gas_lift=True,
        description=__doc__,
    )


if __name__ == "__main__":
    raise SystemExit(main())
