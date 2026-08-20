"""Tests for the generic controls list: constants and swept sensitivities."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
import yaml

import mbal_core as mbal


def tank(key: str, name: str, index: int, official: float) -> mbal.TankConfig:
    """Fixed tank: the injection tests are about plumbing, not volumes."""
    return mbal.TankConfig(
        key=key,
        name=name,
        index=index,
        official_stoiip=official,
    )


RATE_TAG = "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE"
MIN_RATE_TAG = "MBAL.MB[0].PREDINP.CONSTRAINT[1].MININJWATRATE"
BHP_TAG = "MBAL.MB[0].PREDWELL[{INJ1}].MAXFBHP"
LIFT_TAG = "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT"


def swept(name: str, tag: str, values: tuple[float, ...]) -> mbal.Control:
    return mbal.Control(name=name, tag=tag, values=values)


def constant(name: str, tag: str, value: float | str) -> mbal.Control:
    return mbal.Control(name=name, tag=tag, value=value)


def inj_cfg(
    *,
    rate_values: tuple[float, ...] = (0.0, 300.0, 600.0),
    bhp_values: tuple[float, ...] = (250.0, 300.0),
    lift_values: tuple[float, ...] = (),
    extra: tuple[mbal.Control, ...] = (),
    **kwargs,
) -> mbal.Config:
    base = mbal.default_config()
    controls = []
    if lift_values:
        controls.append(swept("gas_lift_rate", LIFT_TAG, lift_values))
    if rate_values:
        controls.append(swept("water_inj_rate", RATE_TAG, rate_values))
    if bhp_values:
        controls.append(swept("water_inj_bhp", BHP_TAG, bhp_values))
    controls.extend(extra)
    updates = {
        "tanks": (tank("A", "TANK_A", 0, 4.5), tank("B", "TANK_B", 1, 3.0)),
        "n_realizations": 8,
        "seed": 4,
        "controls": tuple(controls),
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


def test_apply_realization_writes_swept_and_constant_controls() -> None:
    """Configured tags are written verbatim; no control-specific logic."""
    cfg = inj_cfg(
        rate_values=(400.0,),
        bhp_values=(),
        extra=(
            constant("water_inj_min_rate", MIN_RATE_TAG, 400.0),
            constant("pred_watinj", "MBAL.MB[0].PREDINP.WATINJ", "YES"),
        ),
    )
    row = pd.Series({"stoiip_A": 4.1, "stoiip_B": 2.7, "water_inj_rate": 400.0})
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert (RATE_TAG, 400.0) in server.values
    assert (MIN_RATE_TAG, 400.0) in server.values
    assert ("MBAL.MB[0].PREDINP.WATINJ", "YES") in server.values


def test_a_swept_control_absent_from_the_row_is_skipped() -> None:
    cfg = inj_cfg(rate_values=(400.0,), bhp_values=())
    row = pd.Series({"stoiip_A": 4.1, "stoiip_B": 2.7})
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert all(tag != RATE_TAG for tag, _value in server.values)


def test_control_values_must_be_finite() -> None:
    with pytest.raises(ValueError, match="swept values must be finite"):
        mbal.validate_config(inj_cfg(rate_values=(0.0, float("nan"))))


def test_a_control_needs_exactly_one_of_value_or_values() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        mbal.config_from_dict(
            {"controls": [{"name": "lift", "tag": LIFT_TAG, "value": 1.0,
                           "values": [1.0, 2.0]}]}
        )
    with pytest.raises(ValueError, match="exactly one of"):
        mbal.config_from_dict({"controls": [{"name": "lift", "tag": LIFT_TAG}]})


def test_duplicate_and_colliding_control_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate control name"):
        mbal.validate_config(
            inj_cfg(
                rate_values=(1.0,),
                bhp_values=(),
                extra=(swept("water_inj_rate", MIN_RATE_TAG, (2.0,)),),
            )
        )
    with pytest.raises(ValueError, match="collides with a results column"):
        mbal.validate_config(
            inj_cfg(
                rate_values=(),
                bhp_values=(),
                extra=(swept("stoiip_total", LIFT_TAG, (1.0,)),),
            )
        )


def test_unknown_control_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        mbal.config_from_dict(
            {"controls": [{"name": "lift", "tag": LIFT_TAG, "vals": [1.0]}]}
        )


@pytest.mark.parametrize(
    "removed",
    ["gas_lift_values", "water_inj_rate_values", "water_inj_control"],
)
def test_removed_named_control_keys_point_at_the_controls_list(removed: str) -> None:
    with pytest.raises(ValueError, match="controls"):
        mbal.config_from_dict({removed: [1.0]})


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

    table = pd.read_csv(tmp_path / "water_inj_rate_sensitivity.csv")
    assert len(table) == 6
    present = table.loc[
        (table["water_inj_bhp"] == 250.0)
        & table["water_inj_rate"].isin([0.0, 300.0])
    ]
    np.testing.assert_allclose(present["P50"], [1.8, 3.1])
    missing = table.loc[table["n_rows"] == 0]
    assert len(missing) == 4
    assert (missing["n_missing"] == cfg.n_realizations).all()
    assert (tmp_path / "water_inj_rate_sensitivity.png").exists()


def test_water_inj_summary_reports_failures_and_missing_coverage(tmp_path) -> None:
    cfg = inj_cfg(
        out_dir=str(tmp_path),
        n_realizations=3,
        rate_values=(0.0, 300.0),
        bhp_values=(250.0,),
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

    mbal._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_rate_sensitivity.csv")
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
        rate_values=(0.0, 300.0),
        bhp_values=(250.0,),
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

    mbal._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_rate_sensitivity.csv")
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
        lift_values=(0.0, 1.0),
        rate_values=(0.0, 100.0),
        bhp_values=(),
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

    mbal._summarize_sweeps(results, cfg)

    summary = pd.read_csv(tmp_path / "water_inj_rate_sensitivity.csv")
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

    mbal._summarize_sweeps(results, cfg)

    assert not (tmp_path / "water_inj_rate_sensitivity.csv").exists()


def test_yaml_controls_round_trip(tmp_path) -> None:
    path = tmp_path / "wi.yaml"
    assert mbal.main(["--write-example-config", str(path)]) == 0
    cfg = mbal.load_config_yaml(path, base=mbal.default_config())
    mbal.validate_config(cfg)
    assert cfg.tag_mode == "name"
    assert cfg.controls == ()

    swept_cfg = replace(
        cfg,
        n_realizations=3,
        controls=(
            swept("water_inj_rate", RATE_TAG, (0.0, 100.0)),
            swept("water_inj_bhp", BHP_TAG, (250.0,)),
        ),
    )
    assert len(mbal.build_sample_table(swept_cfg)) == 6


def test_dry_run_cli_sweeps_a_configured_control(tmp_path) -> None:
    config = tmp_path / "case.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "mbal_file": r"C:\Work\model.mbi",
                "controls": [
                    {"name": "water_inj_rate", "tag": RATE_TAG, "value": 0.0}
                ],
                "tanks": [
                    {
                        "key": "A",
                        "name": "TANK_A",
                        "index": 0,
                        "result_index": 1,
                        "official_stoiip": 4.5,
                        "p90_stoiip": 3.5,
                        "p10_stoiip": 5.5,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    code = mbal.main(
        [
            "--config", str(config),
            "--dry-run", "--n", "6", "--seed", "2",
            "--out-dir", str(out),
            "--control", "water_inj_rate=0,200",
        ]
    )

    assert code == 0
    samples = pd.read_csv(out / "samples_dry_run.csv")
    assert len(samples) == 12
    assert set(samples["water_inj_rate"]) == {0.0, 200.0}
    assert (out / "summary_percentiles.csv").exists()


def test_unknown_control_on_the_cli_is_rejected(tmp_path) -> None:
    with pytest.raises(SystemExit, match="unknown control"):
        mbal.main(["--dry-run", "--n", "2", "--control", "nope=1,2"])
