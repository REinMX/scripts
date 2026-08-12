"""Tests for independent per-tank probabilistic MBAL sampling."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import mbal_core as core
import mbal_core as mbal


def uniform(low: float, high: float) -> mbal.Distribution:
    return mbal.Distribution(kind="uniform", low=low, high=high)


def fixed(value: float) -> mbal.Distribution:
    return mbal.Distribution(kind="fixed", value=value)


def tank(
    key: str,
    index: int,
    stoiip: mbal.Distribution,
    aquifer: mbal.Distribution | None = None,
) -> mbal.TankConfig:
    return mbal.TankConfig(
        key=key,
        name=f"Tank {key}",
        index=index,
        stoiip=stoiip,
        aquifer_multiplier=aquifer,
    )


def test_tank_stoiip_samples_are_independent_not_a_total_split() -> None:
    cfg = mbal.Config(
        tanks=(
            tank("A", 0, uniform(10.0, 20.0)),
            tank("B", 1, uniform(100.0, 200.0)),
        ),
        n_realizations=5_000,
        seed=17,
        sampling="mc",
    )

    samples = mbal.build_sample_table(cfg)

    u_a = (samples["stoiip_A"] - 10.0) / 10.0
    u_b = (samples["stoiip_B"] - 100.0) / 100.0
    assert abs(float(np.corrcoef(u_a, u_b)[0, 1])) < 0.06
    assert not np.allclose(u_a, u_b)
    np.testing.assert_allclose(
        samples["stoiip_total"], samples["stoiip_A"] + samples["stoiip_B"]
    )
    assert "frac_A" not in samples


def test_each_tank_can_have_a_different_distribution_family() -> None:
    cfg = mbal.Config(
        tanks=(
            tank(
                "A",
                0,
                mbal.Distribution(kind="lognormal", p90=20.0, p10=80.0),
            ),
            tank(
                "B",
                1,
                mbal.Distribution(kind="triangular", low=10.0, mode=25.0, high=60.0),
            ),
        ),
        n_realizations=4_000,
        seed=42,
        sampling="lhs",
    )

    samples = mbal.build_sample_table(cfg)

    assert np.percentile(samples["stoiip_A"], 10) == pytest.approx(20.0, rel=0.01)
    assert np.percentile(samples["stoiip_A"], 90) == pytest.approx(80.0, rel=0.01)
    assert samples["stoiip_B"].between(10.0, 60.0).all()
    assert samples["stoiip_B"].median() == pytest.approx(30.42, rel=0.02)


def test_arbitrary_number_of_tanks_is_supported() -> None:
    cfg = mbal.Config(
        tanks=(
            tank("North", 2, fixed(12.0)),
            tank("Central", 5, uniform(20.0, 30.0)),
            tank("South", 7, fixed(40.0)),
        ),
        n_realizations=25,
        seed=9,
    )

    samples = mbal.build_sample_table(cfg)

    assert list(samples.columns) == [
        "realization",
        "stoiip_North",
        "stoiip_Central",
        "stoiip_South",
        "stoiip_total",
    ]
    np.testing.assert_allclose(samples["stoiip_North"], 12.0)
    np.testing.assert_allclose(samples["stoiip_South"], 40.0)
    np.testing.assert_allclose(
        samples["stoiip_total"],
        samples[["stoiip_North", "stoiip_Central", "stoiip_South"]].sum(axis=1),
    )


def test_aquifer_multiplier_is_configured_and_sampled_per_tank() -> None:
    cfg = mbal.Config(
        tanks=(
            tank("A", 0, fixed(30.0), uniform(0.5, 1.5)),
            tank("B", 1, fixed(50.0), fixed(2.0)),
        ),
        n_realizations=100,
        seed=3,
    )

    samples = mbal.build_sample_table(cfg)

    assert samples["aq_mult_A"].between(0.5, 1.5).all()
    np.testing.assert_allclose(samples["aq_mult_B"], 2.0)


class RecordingServer:
    def __init__(self) -> None:
        self.values: list[tuple[str, float]] = []

    def set(self, tag: str, value: float) -> None:
        self.values.append((tag, value))


def test_apply_realization_uses_each_tanks_index_and_own_values() -> None:
    cfg = mbal.Config(
        tanks=(
            tank("East", 3, fixed(20.0), fixed(1.2)),
            tank("West", 8, fixed(70.0)),
        )
    )
    row = pd.Series({"stoiip_East": 21.0, "aq_mult_East": 1.3, "stoiip_West": 73.0})
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert server.values == [
        ('MBAL.MB[0].TANK[3].OIIP("MMstb")', 21.0),
        ("MBAL.MB[0].TANK[3].AQUIFER.VOLRATIO", 1.3),
        ('MBAL.MB[0].TANK[8].OIIP("MMstb")', 73.0),
    ]


def test_new_result_record_has_stable_columns_before_a_failed_run() -> None:
    cfg = mbal.Config(
        tanks=(tank("East", 3, fixed(20.0)), tank("West", 8, fixed(70.0)))
    )
    row = pd.Series(
        {
            "realization": 4,
            "stoiip_East": 21.0,
            "stoiip_West": 73.0,
            "stoiip_total": 94.0,
        }
    )

    record = mbal.new_result_record(row, cfg)

    assert record["realization"] == 4
    assert set(record).issuperset(
        {
            "np_East",
            "pres_East",
            "wp_East",
            "rf_East",
            "np_West",
            "pres_West",
            "wp_West",
            "rf_West",
            "np_total",
            "rf_total",
            "status",
            "runtime_s",
        }
    )
    assert all(
        np.isnan(record[column])
        for column in ("np_East", "rf_West", "np_total", "rf_total")
    )


def test_duplicate_tank_keys_are_rejected() -> None:
    cfg = mbal.Config(tanks=(tank("A", 0, fixed(10.0)), tank("A", 1, fixed(20.0))))

    with pytest.raises(ValueError, match="tank keys must be unique"):
        mbal.build_sample_table(cfg)


def test_invalid_distribution_parameters_are_rejected() -> None:
    cfg = mbal.Config(
        tanks=(tank("A", 0, mbal.Distribution(kind="lognormal", p90=80.0, p10=20.0)),)
    )

    with pytest.raises(ValueError, match="p90 must be lower than p10"):
        mbal.build_sample_table(cfg)


def test_percentiles_include_std_and_p95_p5() -> None:
    stats = mbal.percentiles(np.array([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert set(stats) >= {"P90", "P50", "P10", "mean", "std", "P95", "P5"}
    assert stats["P50"] == pytest.approx(3.0)
    assert stats["std"] == pytest.approx(np.std([1, 2, 3, 4, 5]))


def test_resume_retries_failed_but_skips_ok(tmp_path) -> None:
    cfg = mbal.Config(
        tanks=(tank("A", 0, fixed(10.0)), tank("B", 1, fixed(20.0))),
        n_realizations=3,
        seed=1,
        out_dir=str(tmp_path),
        out_csv="results.csv",
    )
    samples = mbal.build_sample_table(cfg)
    csv_path = tmp_path / "results.csv"

    # realization 0 ok, 1 failed, 2 missing
    rows = []
    for _, row in samples.iterrows():
        rid = int(row["realization"])
        if rid == 2:
            continue
        record = mbal.new_result_record(row, cfg)
        if rid == 0:
            record["status"] = "ok"
            record["np_A"] = 1.0
            record["np_B"] = 2.0
            record["np_total"] = 3.0
            record["rf_A"] = 0.1
            record["rf_B"] = 0.1
            record["rf_total"] = 0.1
        else:
            record["status"] = "failed: boom"
        rows.append(record)
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    completed = core._completed_realizations(str(csv_path), samples, cfg)
    assert completed == {0}


def test_yaml_config_roundtrip(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    mbal.main(["--write-example-config", str(path)])
    cfg = mbal.load_config_yaml(path)
    mbal.validate_config(cfg)
    assert len(cfg.tanks) == 3
    assert cfg.tag_mode == "name"
    assert cfg.volume_model.kind == "connected_volume"
    samples = mbal.build_sample_table(
        replace(
            cfg,
            n_realizations=5,
            water_inj_rate_values=(),
            water_inj_bhp_values=(),
            gas_lift_values=(),
        )
    )
    assert len(samples) == 5


def test_dry_run_cli(tmp_path) -> None:
    out = tmp_path / "out"
    code = mbal.main(
        ["--dry-run", "--n", "8", "--seed", "2", "--out-dir", str(out)]
    )
    assert code == 0
    assert (out / "samples_dry_run.csv").exists()
    assert (out / "summary_percentiles.csv").exists()


def test_missing_step_count_warns_before_reading_results_index_zero(caplog) -> None:
    """Index 0 is the FIRST step wherever MBAL returns a time series.

    Falling back to it silently would report ~0 cumulative oil for every
    realization while still marking each run 'ok'.
    """
    cfg = mbal.Config(tanks=(tank("A", 0, fixed(10.0)),))

    class StubServer:
        def __init__(self) -> None:
            self.reads: list[str] = []

        def get(self, tag: str) -> float:
            self.reads.append(tag)
            if "COUNT" in tag:
                raise RuntimeError("OpenServer error 1 on DoGet")
            return 1.0

    core._WARNED_ONCE.clear()
    server = StubServer()
    row = pd.Series({"stoiip_A": 10.0})

    with caplog.at_level("WARNING", logger="mbal"):
        results = mbal.read_results(server, cfg, row)

    assert results["np_A"] == 1.0
    assert any("index 0" in message for message in caplog.messages)
    assert any("[0][0]" in read for read in server.reads)

    # Once per run, not once per realization.
    caplog.clear()
    with caplog.at_level("WARNING", logger="mbal"):
        mbal.read_results(server, cfg, row)
    assert not caplog.messages


def test_readable_step_count_reads_the_last_step_without_warning(caplog) -> None:
    cfg = mbal.Config(tanks=(tank("A", 0, fixed(10.0)),))

    class StubServer:
        def get(self, tag: str) -> float:
            return 25.0 if "COUNT" in tag else 1.0

    core._WARNED_ONCE.clear()
    with caplog.at_level("WARNING", logger="mbal"):
        mbal.read_results(StubServer(), cfg, pd.Series({"stoiip_A": 10.0}))
    assert not caplog.messages


def test_gas_lift_tag_uses_the_configured_well_index() -> None:
    """Index tag mode must address the named well, not always PREDWELL[0]."""
    cfg = mbal.Config(
        tag_mode="index",
        tags=dict(mbal.DEFAULT_INDEX_TAGS),
        gas_lift_well="INJ-2",
        gas_lift_well_index=3,
        gas_lift_prediction_index=1,
        gas_lift_values=(0.0, 1.0),
    )
    assert core._gas_lift_tag(cfg) == "MBAL.MB[0].PREDWELL[3][1].GASLIFTRATE"

    named = replace(
        cfg, tag_mode="name", tags=dict(mbal.DEFAULT_NAME_TAGS)
    )
    assert core._gas_lift_tag(named) == "MBAL.MB[0].PREDWELL[INJ-2][1].GASLIFTRATE"


def test_aquifer_distribution_without_its_tag_is_rejected_up_front() -> None:
    """Fail at config time, not once per realization inside the run loop."""
    # Name-mode defaults ship aquifer_volume but not aquifer_mult.
    cfg = mbal.Config(
        tag_mode="name",
        tags=dict(mbal.DEFAULT_NAME_TAGS),
        tanks=(tank("A", 0, fixed(10.0), aquifer=fixed(1.0)),),
    )
    with pytest.raises(ValueError, match="aquifer_mult"):
        mbal.validate_config(cfg)

    # Index-mode defaults ship aquifer_mult but not aquifer_volume.
    cfg = mbal.Config(
        tag_mode="index",
        tags=dict(mbal.DEFAULT_INDEX_TAGS),
        tanks=(
            mbal.TankConfig(
                key="A",
                name="A",
                index=0,
                stoiip=fixed(10.0),
                aquifer_volume=fixed(1.0),
            ),
        ),
    )
    with pytest.raises(ValueError, match="aquifer_volume"):
        mbal.validate_config(cfg)


def test_summarize_only_without_results_reports_instead_of_tracebacking(
    tmp_path, capsys
) -> None:
    code = core.main(
        ["--summarize-only", "--out-dir", str(tmp_path / "nothing-here")]
    )
    assert code == 1
    assert "No results CSV" in capsys.readouterr().err
