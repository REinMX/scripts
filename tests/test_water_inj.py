"""Tests for water-injection rate / BHP sensitivity."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import mbal_core as mbal


def tank(key: str, name: str, index: int, official: float) -> mbal.TankConfig:
    """Fixed tank: the injection tests are about plumbing, not volumes."""
    return mbal.TankConfig(
        key=key,
        name=name,
        index=index,
        official_stoiip=official,
    )


def inj_cfg(**kwargs) -> mbal.Config:
    base = mbal.default_config()
    tanks = (
        tank("A", "TANK_A", 0, 4.5),
        tank("B", "TANK_B", 1, 3.0),
    )
    updates = {
        "tanks": tanks,
        "n_realizations": 8,
        "seed": 4,
        "water_inj_well": "INJ1",
        "water_inj_prediction_index": 1,
        "water_inj_control": "rate_with_bhp_limit",
        "water_inj_rate_values": (0.0, 300.0, 600.0),
        "water_inj_bhp_values": (250.0, 300.0),
    }
    updates.update(kwargs)
    return replace(base, **updates)


class RecordingServer:
    def __init__(self) -> None:
        self.values: list[tuple[str, float | str]] = []

    def set(self, tag: str, value: float | str) -> None:
        self.values.append((tag, value))


def test_water_inj_sweep_pairs_rate_and_bhp_across_volume_samples() -> None:
    samples = mbal.build_sample_table(inj_cfg())

    assert len(samples) == 8 * 3 * 2
    assert samples["realization"].tolist() == list(range(48))
    assert set(samples["water_inj_rate"]) == {0.0, 300.0, 600.0}
    assert set(samples["water_inj_bhp"]) == {250.0, 300.0}
    for _, paired in samples.groupby("base_realization"):
        assert set(paired["water_inj_rate"]) == {0.0, 300.0, 600.0}
        assert set(paired["water_inj_bhp"]) == {250.0, 300.0}
        assert paired["stoiip_A"].nunique() == 1
        assert paired["stoiip_B"].nunique() == 1
        assert paired["stoiip_total"].nunique() == 1


def test_apply_realization_writes_rate_and_bhp_limit_tags() -> None:
    cfg = inj_cfg()
    row = pd.Series(
        {
            "stoiip_A": 4.1,
            "stoiip_B": 2.7,
            "water_inj_rate": 300.0,
            "water_inj_bhp": 280.0,
        }
    )
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert ("MBAL.MB[0].PREDINP.WATINJ", "YES") in server.values
    assert (
        "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE",
        300.0,
    ) in server.values
    assert ("MBAL.MB[0].PREDWELL[{INJ1}].MAXFBHP", 280.0) in server.values
    assert ("MBAL.MB[0].TANK[{TANK_A}].OOIP", 4.1) in server.values
    assert ("MBAL.MB[0].TANK[{TANK_B}].OOIP", 2.7) in server.values
    assert not any(".MAXRATE" in tag for tag, _value in server.values)


def test_apply_realization_bhp_control_sets_cfbhp() -> None:
    cfg = inj_cfg(
        water_inj_control="bhp",
        water_inj_rate_values=(),
        water_inj_bhp_values=(260.0,),
    )
    row = pd.Series(
        {
            "stoiip_A": 4.1,
            "stoiip_B": 2.7,
            "water_inj_bhp": 260.0,
        }
    )
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert ("MBAL.MB[0].PREDWELL[{INJ1}].PERFORMTYPE", "CFBHP") in server.values
    assert ("MBAL.MB[0].PREDWELL[{INJ1}].CONSTFBHP", 260.0) in server.values


def test_rate_control_pins_min_rate() -> None:
    cfg = inj_cfg(
        water_inj_control="rate",
        water_inj_bhp_values=(),
        water_inj_rate_values=(400.0,),
    )
    row = pd.Series(
        {
            "stoiip_A": 4.1,
            "stoiip_B": 2.7,
            "water_inj_rate": 400.0,
        }
    )
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert (
        "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE",
        400.0,
    ) in server.values
    assert (
        "MBAL.MB[0].PREDINP.CONSTRAINT[1].MININJWATRATE",
        400.0,
    ) in server.values


def test_negative_rate_and_nonpositive_bhp_are_rejected() -> None:
    with pytest.raises(ValueError, match="water-injection rate"):
        mbal.validate_config(inj_cfg(water_inj_rate_values=(0.0, -10.0)))
    with pytest.raises(ValueError, match="water-injection BHP"):
        mbal.validate_config(inj_cfg(water_inj_bhp_values=(0.0, 250.0)))


def test_water_inj_summary_does_not_pool_across_settings(tmp_path) -> None:
    cfg = inj_cfg(out_dir=str(tmp_path))
    results = pd.DataFrame(
        {
            "realization": [0, 1, 2, 3],
            "base_realization": [0, 1, 0, 1],
            "water_inj_rate": [0.0, 0.0, 300.0, 300.0],
            "water_inj_bhp": [250.0, 250.0, 250.0, 250.0],
            "stoiip_A": [4.0, 5.0, 4.0, 5.0],
            "stoiip_B": [2.5, 3.5, 2.5, 3.5],
            "stoiip_total": [6.5, 8.5, 6.5, 8.5],
            "np_A": [1.0, 1.2, 1.8, 2.0],
            "np_B": [0.6, 0.8, 1.1, 1.3],
            "np_total": [1.6, 2.0, 2.9, 3.3],
            "status": ["ok", "ok", "ok", "ok"],
        }
    )

    summary = mbal.summarize(results, cfg)
    variables = summary["variable"].tolist()
    assert variables == [
        "TANK_A STOIIP [MSm3]",
        "TANK_B STOIIP [MSm3]",
        "Field STOIIP [MSm3]",
    ]

    table = pd.read_csv(tmp_path / "water_inj_sensitivity.csv")
    assert len(table) == 6
    present = table.loc[
        (table["water_inj_bhp"] == 250.0)
        & table["water_inj_rate"].isin([0.0, 300.0])
    ]
    np.testing.assert_allclose(present["P50"], [1.8, 3.1])
    missing = table.loc[table["n_rows"] == 0]
    assert len(missing) == 4
    assert (missing["n_missing"] == cfg.n_realizations).all()
    assert (tmp_path / "water_inj_sensitivity.png").exists()


def test_water_inj_summary_reports_failures_and_missing_coverage(tmp_path) -> None:
    cfg = inj_cfg(
        out_dir=str(tmp_path),
        n_realizations=3,
        water_inj_rate_values=(0.0, 300.0),
        water_inj_bhp_values=(250.0,),
    )
    results = pd.DataFrame(
        {
            "realization": [0, 1, 2, 3, 4],
            "base_realization": [0, 1, 2, 0, 1],
            "water_inj_rate": [0.0, 0.0, 0.0, 300.0, 300.0],
            "water_inj_bhp": [250.0] * 5,
            "np_total": [1.0, np.nan, 1.4, 1.8, 2.0],
            "status": ["ok", "failed: convergence", "ok", "ok", "ok"],
        }
    )

    mbal._summarize_water_inj(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_sensitivity.csv")
    baseline = summary.loc[summary["water_inj_rate"] == 0.0].iloc[0]
    higher_rate = summary.loc[summary["water_inj_rate"] == 300.0].iloc[0]
    assert baseline["n_expected"] == 3
    assert baseline["n_rows"] == 3
    assert baseline["n_ok"] == 2
    assert baseline["n_failed"] == 1
    assert baseline["n_missing"] == 0
    assert baseline["success_fraction"] == pytest.approx(2.0 / 3.0)
    assert higher_rate["n_expected"] == 3
    assert higher_rate["n_rows"] == 2
    assert higher_rate["n_ok"] == 2
    assert higher_rate["n_failed"] == 0
    assert higher_rate["n_missing"] == 1
    assert higher_rate["success_fraction"] == pytest.approx(2.0 / 3.0)


def test_water_inj_summary_reports_a_completely_missing_setting(tmp_path) -> None:
    cfg = inj_cfg(
        out_dir=str(tmp_path),
        n_realizations=3,
        water_inj_rate_values=(0.0, 300.0),
        water_inj_bhp_values=(250.0,),
    )
    results = pd.DataFrame(
        {
            "realization": [0, 1, 2],
            "base_realization": [0, 1, 2],
            "water_inj_rate": [0.0, 0.0, 0.0],
            "water_inj_bhp": [250.0, 250.0, 250.0],
            "np_total": [1.0, 1.2, 1.4],
            "status": ["ok", "ok", "ok"],
        }
    )

    mbal._summarize_water_inj(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_sensitivity.csv")
    missing_setting = summary.loc[summary["water_inj_rate"] == 300.0].iloc[0]
    assert len(summary) == 2
    assert missing_setting["n_rows"] == 0
    assert missing_setting["n_ok"] == 0
    assert missing_setting["n_missing"] == 3
    assert missing_setting["success_fraction"] == pytest.approx(0.0)
    assert missing_setting["n_paired"] == 0


def test_water_inj_summary_does_not_pool_gas_lift_settings(tmp_path) -> None:
    cfg = inj_cfg(
        out_dir=str(tmp_path),
        n_realizations=2,
        gas_lift_values=(0.0, 1.0),
        water_inj_rate_values=(0.0, 100.0),
        water_inj_bhp_values=(),
    )
    results = pd.DataFrame(
        {
            "realization": range(8),
            "base_realization": [0, 1, 0, 1, 0, 1, 0, 1],
            "gas_lift_rate": [0.0] * 4 + [1.0] * 4,
            "water_inj_rate": [0.0, 0.0, 100.0, 100.0] * 2,
            "np_total": [10.0, 20.0, 12.0, 22.0, 15.0, 25.0, 20.0, 30.0],
            "status": ["ok"] * 8,
        }
    )

    mbal._summarize_water_inj(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_sensitivity.csv")
    assert len(summary) == 4
    higher_rate_with_lift = summary.loc[
        (summary["water_inj_rate"] == 100.0)
        & (summary["gas_lift_rate"] == 1.0)
    ].iloc[0]
    assert higher_rate_with_lift["n_rows"] == 2
    assert higher_rate_with_lift["delta_P50"] == pytest.approx(5.0)


def test_water_inj_summary_handles_an_empty_results_table(tmp_path) -> None:
    cfg = inj_cfg(out_dir=str(tmp_path))
    results = pd.DataFrame(
        columns=[
            "realization",
            "base_realization",
            "water_inj_rate",
            "water_inj_bhp",
            "np_total",
            "status",
        ]
    )

    mbal._summarize_water_inj(results, cfg)

    assert not (tmp_path / "water_inj_sensitivity.csv").exists()


def test_yaml_water_inj_config(tmp_path) -> None:
    path = tmp_path / "wi.yaml"
    assert mbal.main(["--write-example-config", str(path)]) == 0
    cfg = mbal.load_config_yaml(path, base=mbal.default_config())
    mbal.validate_config(cfg)
    assert cfg.tag_mode == "name"
    assert cfg.water_inj_rate_values == ()
    assert cfg.water_inj_bhp_values == ()
    samples = mbal.build_sample_table(
        replace(
            cfg,
            n_realizations=3,
            gas_lift_values=(),
            water_inj_rate_values=(0.0, 100.0),
            water_inj_bhp_values=(250.0,),
        )
    )
    assert len(samples) == 6


def test_dry_run_cli(tmp_path) -> None:
    out = tmp_path / "out"
    code = mbal.main(
        [
            "--dry-run",
            "--n",
            "6",
            "--seed",
            "2",
            "--out-dir",
            str(out),
            "--water-inj-rate-values",
            "0,200",
            "--water-inj-bhp-values",
            "270",
        ]
    )
    assert code == 0
    samples = pd.read_csv(out / "samples_dry_run.csv")
    assert len(samples) == 12
    assert set(samples["water_inj_rate"]) == {0.0, 200.0}
    assert set(samples["water_inj_bhp"]) == {270.0}
    assert (out / "summary_percentiles.csv").exists()
