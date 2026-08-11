"""
Probabilistic MBAL via Petroleum Experts OpenServer
===================================================

Independent per-tank STOIIP sampling (MC/LHS). Field STOIIP is a derived sum.

    STOIIP_A     ~ Tank A distribution
    STOIIP_B     ~ Tank B distribution
    STOIIP_total = STOIIP_A + STOIIP_B

Usage
-----
    python probabilistic_mbal_openserver.py --dry-run --n 1000
    python probabilistic_mbal_openserver.py --config example_config.yaml --n 500
    python probabilistic_mbal_openserver.py --model C:\\Models\\field.mbi --n 500
    python probabilistic_mbal_openserver.py --summarize-only
    python probabilistic_mbal_openserver.py --write-example-config example_config.yaml

Implementation lives in mbal_core.py.
"""

from __future__ import annotations

from mbal_core import (  # noqa: F401
    CFG,
    DEFAULT_INDEX_TAGS,
    DEFAULT_NAME_TAGS,
    TAGS,
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


def main(argv: list[str] | None = None) -> int:
    return _core_main(
        argv,
        gas_lift=False,
        description=__doc__,
    )


if __name__ == "__main__":
    raise SystemExit(main())
