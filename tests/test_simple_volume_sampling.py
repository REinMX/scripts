"""Tests for the simplified per-tank STOIIP model."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

import mbal_core as mbal


def test_tank_without_percentiles_uses_fixed_official_volume() -> None:
    cfg = mbal.Config(
        tanks=(
            mbal.TankConfig(
                key="A",
                name="Tank A",
                index=0,
                official_stoiip=4.5,
            ),
        ),
        n_realizations=25,
        seed=7,
    )

    samples = mbal.build_sample_table(cfg)

    np.testing.assert_allclose(samples["stoiip_A"], 4.5)
    np.testing.assert_allclose(samples["stoiip_total"], 4.5)


def test_optional_percentiles_anchor_p90_official_p50_and_p10() -> None:
    cfg = mbal.Config(
        tanks=(
            mbal.TankConfig(
                key="A",
                name="Tank A",
                index=0,
                official_stoiip=10.0,
                p90_stoiip=6.0,
                p10_stoiip=18.0,
            ),
        ),
        n_realizations=10_000,
        seed=17,
        sampling="lhs",
    )

    samples = mbal.build_sample_table(cfg)

    values = samples["stoiip_A"].to_numpy(dtype=float)
    assert np.percentile(values, 10) == pytest.approx(6.0, rel=0.01)
    assert np.percentile(values, 50) == pytest.approx(10.0, rel=0.01)
    assert np.percentile(values, 90) == pytest.approx(18.0, rel=0.01)


def test_yaml_loads_optional_tank_percentiles(tmp_path) -> None:
    path = tmp_path / "case.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "n_realizations": 2_000,
                "seed": 9,
                "tanks": [
                    {
                        "key": "A",
                        "name": "Tank A",
                        "index": 0,
                        "official_stoiip": 10.0,
                        "p90_stoiip": 6.0,
                        "p10_stoiip": 18.0,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cfg = mbal.load_config_yaml(path)
    samples = mbal.build_sample_table(cfg)

    assert cfg.tanks[0].p90_stoiip == 6.0
    assert cfg.tanks[0].p10_stoiip == 18.0
    assert np.percentile(samples["stoiip_A"], 50) == pytest.approx(10.0, rel=0.02)


def test_removed_volume_model_is_rejected_instead_of_reinterpreted() -> None:
    with pytest.raises(ValueError, match="volume_model.*removed"):
        mbal.config_from_dict(
            {
                "volume_model": {
                    "kind": "connected_volume",
                    "connectivity_correlation": 0.8,
                }
            }
        )


@pytest.mark.parametrize("removed_key", ["connectivity", "residual", "role"])
def test_removed_tank_volume_keys_are_rejected(removed_key: str) -> None:
    tank = {
        "key": "A",
        "name": "Tank A",
        "index": 0,
        "official_stoiip": 4.5,
        removed_key: {} if removed_key != "role" else "base",
    }

    with pytest.raises(ValueError, match=f"{removed_key}.*removed"):
        mbal.config_from_dict({"tanks": [tank]})


def test_default_config_is_three_simple_in_model_tanks() -> None:
    cfg = mbal.default_config()

    assert [tank.key for tank in cfg.tanks] == ["A", "B", "C"]
    assert [tank.official_stoiip for tank in cfg.tanks] == [4.5, 3.0, 6.5]
    assert [tank.p90_stoiip for tank in cfg.tanks] == [3.5, None, 5.0]
    assert [tank.p10_stoiip for tank in cfg.tanks] == [5.5, None, 8.0]
    assert all(tank.in_model for tank in cfg.tanks)
    assert not hasattr(cfg, "volume_model")
    assert not hasattr(cfg.tanks[0], "connectivity")


def test_write_example_config_emits_only_the_normal_user_inputs(tmp_path) -> None:
    path = tmp_path / "example.yaml"

    assert mbal.main(["--write-example-config", str(path)]) == 0

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(data) == {
        "mbal_file",
        "tag_mode",
        "n_realizations",
        "seed",
        "sampling",
        "out_dir",
        "gas_lift_values",
        "water_inj_control",
        "water_inj_rate_values",
        "water_inj_bhp_values",
        "tanks",
    }
    assert len(data["tanks"]) == 3
    assert data["water_inj_rate_values"] == []
    assert data["water_inj_bhp_values"] == []
    assert "tags" not in data


def test_three_tanks_are_sampled_independently_and_summed_row_by_row() -> None:
    tanks = tuple(
        mbal.TankConfig(
            key=key,
            name=f"Tank {key}",
            index=index,
            official_stoiip=official,
            p90_stoiip=official * 0.7,
            p10_stoiip=official * 1.4,
        )
        for index, (key, official) in enumerate(
            (("A", 10.0), ("B", 20.0), ("C", 30.0))
        )
    )
    cfg = mbal.Config(tanks=tanks, n_realizations=10_000, seed=3, sampling="lhs")

    samples = mbal.build_sample_table(cfg)

    np.testing.assert_allclose(
        samples["stoiip_total"],
        samples[["stoiip_A", "stoiip_B", "stoiip_C"]].sum(axis=1),
    )
    rank_correlation = samples[["stoiip_A", "stoiip_B", "stoiip_C"]].rank().corr()
    off_diagonal = rank_correlation.to_numpy()[~np.eye(3, dtype=bool)]
    assert np.max(np.abs(off_diagonal)) < 0.04
    assert set(samples.columns) == {
        "realization",
        "stoiip_A",
        "stoiip_B",
        "stoiip_C",
        "stoiip_total",
    }
    assert not any(
        column.startswith(("connected_", "connect_frac_", "residual_"))
        for column in samples.columns
    )
    assert "field_scale" not in samples


@pytest.mark.parametrize(
    ("p90", "official", "p10", "message"),
    [
        (5.0, 10.0, None, "must be given together"),
        (None, 10.0, 15.0, "must be given together"),
        (10.0, 10.0, 15.0, "require 0 <"),
        (5.0, 10.0, 10.0, "require 0 <"),
        (-1.0, 10.0, 15.0, "require 0 <"),
    ],
)
def test_tank_percentile_contract_is_validated(
    p90: float | None, official: float, p10: float | None, message: str
) -> None:
    cfg = mbal.Config(
        tanks=(
            mbal.TankConfig(
                key="A",
                name="Tank A",
                index=0,
                official_stoiip=official,
                p90_stoiip=p90,
                p10_stoiip=p10,
            ),
        )
    )

    with pytest.raises(ValueError, match=message):
        mbal.validate_config(cfg)


def test_summary_reports_official_alongside_volume_percentiles(tmp_path) -> None:
    cfg = mbal.Config(
        tanks=(
            mbal.TankConfig(
                key="A",
                name="Tank A",
                index=0,
                official_stoiip=10.0,
                p90_stoiip=6.0,
                p10_stoiip=18.0,
            ),
            mbal.TankConfig(
                key="B",
                name="Tank B",
                index=1,
                official_stoiip=3.0,
            ),
        ),
        n_realizations=2_000,
        seed=2,
        out_dir=str(tmp_path),
    )
    samples = mbal.build_sample_table(cfg)

    summary = mbal.summarize(samples, cfg)

    assert summary["official"].tolist()[:3] == [10.0, 3.0, 13.0]
    written = pd.read_csv(tmp_path / "summary_percentiles.csv")
    assert written["official"].tolist()[:3] == [10.0, 3.0, 13.0]
    assert not (tmp_path / "decision_volume_summary.csv").exists()
