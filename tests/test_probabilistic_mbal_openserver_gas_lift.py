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


def tank(
    key: str,
    name: str,
    index: int,
    official: float,
    aquifer: mbal.Distribution | None = None,
    *,
    p90: float | None = None,
    p10: float | None = None,
) -> mbal.TankConfig:
    """Tank with a fixed or direct P90/P50/P10 volume prior."""
    return mbal.TankConfig(
        key=key,
        name=name,
        index=index,
        official_stoiip=official,
        p90_stoiip=p90,
        p10_stoiip=p10,
        aquifer_volume=aquifer,
    )


LIFT_TAG = "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT"


def lift(values: tuple[float, ...]) -> tuple[mbal.Control, ...]:
    return (mbal.Control(name="gas_lift_rate", tag=LIFT_TAG, values=values),)


def name_cfg(**kwargs) -> mbal.Config:
    """Name-based tags for gas-lift/OpenServer plumbing tests."""
    base = replace(
        mbal.default_config(),
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
            tank("bottom", "BOTTOM_TANK", 0, 15.0, p90=10.0, p10=20.0),
            tank("top", "TOP_TANK", 1, 40.0, p90=30.0, p10=50.0),
        ),
        n_realizations=12,
        seed=7,
        sampling="lhs",
        controls=lift((0.0, 0.5, 1.0)),
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
        controls=lift((0.5,)),
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
        ("MBAL.MB[0].TANK[{BOTTOM_TANK}].OOIP", 21.0),
        ("MBAL.MB[0].TANK[{BOTTOM_TANK}].AQUIF.VOLUME", 110.0),
        ("MBAL.MB[0].TANK[{TOP_TANK}].OOIP", 42.0),
        ("MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT", 0.5),
    ]


def test_a_control_tag_must_not_be_empty() -> None:
    cfg = name_cfg(
        controls=(mbal.Control(name="gas_lift_rate", tag="  ", values=(0.0, 1.0)),)
    )

    with pytest.raises(ValueError, match="tag must not be empty"):
        mbal.validate_config(cfg)


def test_gas_lift_summary_reports_field_oil_percentiles(tmp_path) -> None:
    cfg = name_cfg(out_dir=str(tmp_path), controls=lift((0.0, 1.0)))
    results = pd.DataFrame(
        {
            "gas_lift_rate": [0.0, 0.0, 1.0, 1.0],
            "np_total": [10.0, 20.0, 30.0, 50.0],
            "status": ["ok", "ok", "ok", "ok"],
        }
    )

    core._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "gas_lift_rate_sensitivity.csv")
    assert summary["gas_lift_rate"].tolist() == [0.0, 1.0]
    np.testing.assert_allclose(summary["P50"], [15.0, 40.0])
    np.testing.assert_allclose(summary["mean"], [15.0, 40.0])
    assert (tmp_path / "gas_lift_rate_sensitivity.png").exists()


def test_gas_lift_summary_reports_paired_incremental_oil(tmp_path) -> None:
    cfg = name_cfg(
        out_dir=str(tmp_path),
        n_realizations=3,
        controls=lift((0.0, 1.0)),
    )
    results = pd.DataFrame(
        {
            "realization": [0, 1, 2, 3, 4, 5],
            "base_realization": [0, 1, 2, 0, 1, 2],
            "gas_lift_rate": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            "np_total": [10.0, 20.0, 30.0, 15.0, 18.0, 45.0],
            "status": ["ok"] * 6,
        }
    )

    core._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "gas_lift_rate_sensitivity.csv")
    baseline = summary.loc[summary["gas_lift_rate"] == 0.0].iloc[0]
    higher_lift = summary.loc[summary["gas_lift_rate"] == 1.0].iloc[0]
    assert baseline["reference_gas_lift_rate"] == pytest.approx(0.0)
    assert baseline["delta_P50"] == pytest.approx(0.0)
    assert higher_lift["reference_gas_lift_rate"] == pytest.approx(0.0)
    assert higher_lift["n_paired"] == 3
    assert higher_lift["delta_P50"] == pytest.approx(5.0)
    assert higher_lift["probability_delta_positive"] == pytest.approx(2.0 / 3.0)


def test_gas_lift_summary_does_not_pool_water_injection_settings(tmp_path) -> None:
    cfg = name_cfg(
        out_dir=str(tmp_path),
        n_realizations=2,
        controls=(
            *lift((0.0, 1.0)),
            mbal.Control(
                name="water_inj_rate",
                tag="MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE",
                values=(0.0, 100.0),
            ),
        ),
    )
    results = pd.DataFrame(
        {
            "realization": range(8),
            "base_realization": [0, 1, 0, 1, 0, 1, 0, 1],
            "gas_lift_rate": [0.0, 0.0, 1.0, 1.0] * 2,
            "water_inj_rate": [0.0] * 4 + [100.0] * 4,
            "np_total": [10.0, 20.0, 15.0, 25.0, 12.0, 22.0, 20.0, 30.0],
            "status": ["ok"] * 8,
        }
    )

    core._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "gas_lift_rate_sensitivity.csv")
    assert len(summary) == 4
    higher_lift_with_injection = summary.loc[
        (summary["gas_lift_rate"] == 1.0)
        & (summary["water_inj_rate"] == 100.0)
    ].iloc[0]
    assert higher_lift_with_injection["n_rows"] == 2
    assert higher_lift_with_injection["delta_P50"] == pytest.approx(8.0)


def test_main_summary_does_not_pool_results_across_gas_lift_rates(tmp_path) -> None:
    cfg = name_cfg(out_dir=str(tmp_path), controls=lift((0.0, 1.0)))
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
    samples = mbal.build_sample_table(
        replace(
            cfg,
            n_realizations=4,
            controls=lift((0.0, 1.0)),
        )
    )
    assert len(samples) == 8
