"""Tests for water-injection rate / BHP sensitivity."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import mbal_core as mbal


def tank(key: str, name: str, index: int, official: float) -> mbal.TankConfig:
    """Always-connected tank: the injection tests are about plumbing, not volumes."""
    return mbal.TankConfig(
        key=key,
        name=name,
        index=index,
        official_stoiip=official,
        connectivity=mbal.Connectivity(
            kind="two_section",
            p_connected=1.0,
            isolated_fraction=0.5,
            residual=mbal.Distribution(kind="lognormal", p90=0.85, p10=1.12),
        ),
    )


def inj_cfg(**kwargs) -> mbal.Config:
    base = mbal.default_config()
    tanks = (
        tank("A", "TANK_A", 0, 4.5),
        tank("B", "TANK_B", 1, 3.0),
    )
    updates = {
        "tanks": tanks,
        "volume_model": mbal.VolumeModel(
            field_scale=mbal.Distribution(kind="lognormal", p90=0.70, p10=1.18),
        ),
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
    assert ("MBAL.MB[0].PREDWELL[INJ1][1].MAXRATE", 300.0) in server.values
    assert (
        "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE",
        300.0,
    ) in server.values
    assert ("MBAL.MB[0].PREDWELL[INJ1].MAXFBHP", 280.0) in server.values
    assert ("MBAL.MB[0].TANK[TANK_A].OOIP", 4.1) in server.values
    assert ("MBAL.MB[0].TANK[TANK_B].OOIP", 2.7) in server.values


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

    assert ("MBAL.MB[0].PREDWELL[INJ1].PERFORMTYPE", "CFBHP") in server.values
    assert ("MBAL.MB[0].PREDWELL[INJ1].CONSTFBHP", 260.0) in server.values


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

    assert ("MBAL.MB[0].PREDWELL[INJ1][1].MAXRATE", 400.0) in server.values
    assert ("MBAL.MB[0].PREDWELL[INJ1][1].MINRATE", 400.0) in server.values


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
    assert table["water_inj_rate"].tolist() == [0.0, 300.0]
    np.testing.assert_allclose(table["P50"], [1.8, 3.1])
    assert (tmp_path / "water_inj_sensitivity.png").exists()


def test_yaml_water_inj_config(tmp_path) -> None:
    path = tmp_path / "wi.yaml"
    assert mbal.main(["--write-example-config", str(path)]) == 0
    cfg = mbal.load_config_yaml(path, base=mbal.default_config())
    mbal.validate_config(cfg)
    assert cfg.tag_mode == "name"
    assert cfg.water_inj_rate_values
    assert cfg.water_inj_bhp_values
    assert cfg.volume_model.kind == "connected_volume"
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
