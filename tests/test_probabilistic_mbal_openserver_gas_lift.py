"""Tests for the model-specific MBAL gas-lift sensitivity script."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import mbal_core as core
import mbal_core as mbal


def fixed(value: float) -> mbal.Distribution:
    return mbal.Distribution(kind="fixed", value=value)


def uniform(low: float, high: float) -> mbal.Distribution:
    return mbal.Distribution(kind="uniform", low=low, high=high)


def tank(
    key: str,
    name: str,
    index: int,
    official: float,
    aquifer: mbal.Distribution | None = None,
    *,
    residual: mbal.Distribution | None = None,
) -> mbal.TankConfig:
    """Always-connected tank, so stoiip_<key> == official x residual."""
    return mbal.TankConfig(
        key=key,
        name=name,
        index=index,
        official_stoiip=official,
        aquifer_volume=aquifer,
        connectivity=mbal.Connectivity(
            kind="two_section",
            p_connected=1.0,
            isolated_fraction=0.5,
            residual=residual if residual is not None else fixed(1.0),
        ),
    )


def name_cfg(**kwargs) -> mbal.Config:
    """Name-based tags, no shared field_scale unless a test asks for one."""
    base = replace(
        mbal.default_config(),
        volume_model=mbal.VolumeModel(),
        unit_stoiip="MMstb",
        unit_cum="MMstb",
        unit_press="psig",
        tanks=(
            tank("bottom", "REPLACE_WITH_BOTTOM_TANK_NAME", 0, 20.0),
            tank("top", "REPLACE_WITH_TOP_TANK_NAME", 1, 40.0),
        ),
    )
    return replace(base, **kwargs) if kwargs else base


def test_gas_lift_sweep_reuses_each_probabilistic_realization() -> None:
    cfg = name_cfg(
        tanks=(
            tank("bottom", "BOTTOM_TANK", 0, 1.0, residual=uniform(10.0, 20.0)),
            tank("top", "TOP_TANK", 1, 1.0, residual=uniform(30.0, 50.0)),
        ),
        n_realizations=12,
        seed=7,
        sampling="lhs",
        gas_lift_values=(0.0, 0.5, 1.0),
    )

    samples = mbal.build_sample_table(cfg)

    assert len(samples) == 36
    assert samples["realization"].tolist() == list(range(36))
    assert set(samples["gas_lift_rate"]) == {0.0, 0.5, 1.0}
    for _, paired in samples.groupby("base_realization"):
        assert set(paired["gas_lift_rate"]) == {0.0, 0.5, 1.0}
        assert paired["stoiip_bottom"].nunique() == 1
        assert paired["stoiip_top"].nunique() == 1
        assert paired["stoiip_total"].nunique() == 1


class RecordingServer:
    def __init__(self) -> None:
        self.values: list[tuple[str, float]] = []

    def set(self, tag: str, value: float) -> None:
        self.values.append((tag, value))


def test_apply_realization_uses_verified_name_based_input_tags() -> None:
    cfg = name_cfg(
        tanks=(
            tank("bottom", "BOTTOM_TANK", 0, 20.0, fixed(100.0)),
            tank("top", "TOP_TANK", 1, 40.0),
        ),
        gas_lift_well="LIFTED_WELL",
        gas_lift_prediction_index=1,
        gas_lift_values=(0.5,),
    )
    row = pd.Series(
        {
            "stoiip_bottom": 21.0,
            "aquifer_volume_bottom": 110.0,
            "stoiip_top": 42.0,
            "gas_lift_rate": 0.5,
        }
    )
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert server.values == [
        ("MBAL.MB[0].TANK[BOTTOM_TANK].OOIP", 21.0),
        ("MBAL.MB[0].TANK[BOTTOM_TANK].AQUIFVOLUME", 110.0),
        ("MBAL.MB[0].TANK[TOP_TANK].OOIP", 42.0),
        ("MBAL.MB[0].PREDWELL[LIFTED_WELL][1].GASLIFTRATE", 0.5),
    ]


def test_negative_gas_lift_value_is_rejected() -> None:
    cfg = name_cfg(gas_lift_values=(0.0, -0.5))

    with pytest.raises(ValueError, match="gas-lift sensitivity values"):
        mbal.validate_config(cfg)


def test_gas_lift_summary_reports_field_oil_percentiles(tmp_path) -> None:
    cfg = name_cfg(out_dir=str(tmp_path), gas_lift_values=(0.0, 1.0))
    results = pd.DataFrame(
        {
            "gas_lift_rate": [0.0, 0.0, 1.0, 1.0],
            "np_total": [10.0, 20.0, 30.0, 50.0],
            "status": ["ok", "ok", "ok", "ok"],
        }
    )

    core._summarize_gas_lift(results, cfg)

    summary = pd.read_csv(tmp_path / "gas_lift_sensitivity.csv")
    assert summary["gas_lift_rate"].tolist() == [0.0, 1.0]
    np.testing.assert_allclose(summary["P50"], [15.0, 40.0])
    np.testing.assert_allclose(summary["mean"], [15.0, 40.0])
    assert (tmp_path / "gas_lift_sensitivity.png").exists()


def test_main_summary_does_not_pool_results_across_gas_lift_rates(tmp_path) -> None:
    cfg = name_cfg(out_dir=str(tmp_path), gas_lift_values=(0.0, 1.0))
    results = pd.DataFrame(
        {
            "realization": [0, 1, 2, 3],
            "base_realization": [0, 1, 0, 1],
            "gas_lift_rate": [0.0, 0.0, 1.0, 1.0],
            "stoiip_bottom": [20.0, 30.0, 20.0, 30.0],
            "stoiip_top": [40.0, 50.0, 40.0, 50.0],
            "stoiip_total": [60.0, 80.0, 60.0, 80.0],
            "np_bottom": [5.0, 7.0, 9.0, 12.0],
            "np_top": [8.0, 10.0, 14.0, 18.0],
            "np_total": [13.0, 17.0, 23.0, 30.0],
            "rf_bottom": [0.25, 0.23, 0.45, 0.40],
            "rf_top": [0.20, 0.20, 0.35, 0.36],
            "rf_total": [0.22, 0.21, 0.38, 0.38],
            "status": ["ok", "ok", "ok", "ok"],
        }
    )

    summary = mbal.summarize(results, cfg)

    variables = summary["variable"].tolist()
    assert variables == [
        "REPLACE_WITH_BOTTOM_TANK_NAME STOIIP [MMstb]",
        "REPLACE_WITH_TOP_TANK_NAME STOIIP [MMstb]",
        "Field STOIIP [MMstb]",
    ]


def test_yaml_gas_lift_config(tmp_path) -> None:
    path = tmp_path / "gl.yaml"
    mbal.main(["--write-example-config", str(path)])
    cfg = mbal.load_config_yaml(path, base=mbal.default_config())
    mbal.validate_config(cfg)
    assert cfg.tag_mode == "name"
    assert len(cfg.tanks) == 3
    assert cfg.tanks[2].role == "upside"
    samples = mbal.build_sample_table(
        replace(
            cfg,
            n_realizations=4,
            gas_lift_values=(0.0, 1.0),
            water_inj_rate_values=(),
            water_inj_bhp_values=(),
        )
    )
    assert len(samples) == 8


def test_legacy_script_modules_reexport_the_shared_api() -> None:
    import probabilistic_mbal_openserver as legacy
    import probabilistic_mbal_openserver_gas_lift as legacy_gas_lift

    assert legacy.Config is core.Config
    assert legacy_gas_lift.Config is core.Config
    assert callable(legacy.main)
    assert callable(legacy_gas_lift.main)
