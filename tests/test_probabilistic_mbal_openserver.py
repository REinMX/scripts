"""Tests for independent per-tank probabilistic MBAL sampling."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import mbal_core as core
import probabilistic_mbal_openserver as mbal


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
    assert len(cfg.tanks) == 2
    assert cfg.tag_mode == "index"
    samples = mbal.build_sample_table(
        mbal.Config(
            tanks=cfg.tanks,
            n_realizations=5,
            seed=cfg.seed,
            sampling=cfg.sampling,
            tags=cfg.tags,
            tag_mode=cfg.tag_mode,
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
