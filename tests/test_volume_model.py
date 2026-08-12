"""Tests for FMU/ERT residual oil-in-place uncertainty."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

import mbal_core as core
import mbal_core as mbal


def tank(
    key: str,
    index: int,
    *,
    official: float | None = None,
    stoiip: mbal.Distribution | None = None,
    residual: mbal.Distribution | None = None,
) -> mbal.TankConfig:
    return mbal.TankConfig(
        key=key,
        name=f"Tank {key}",
        index=index,
        stoiip=stoiip,
        official_stoiip=official,
        residual=residual,
    )


def fmu_cfg(**kwargs) -> mbal.Config:
    base = mbal.Config(
        tanks=(
            tank("A", 0, official=4.5),
            tank("B", 1, official=3.0),
        ),
        volume_model=mbal.VolumeModel(
            kind="fmu_residual",
            official_as="p40",
            field_scale=mbal.Distribution(kind="lognormal", p90=0.70, p10=1.18),
            residual=mbal.Distribution(kind="lognormal", p90=0.85, p10=1.12),
        ),
        n_realizations=4_000,
        seed=11,
        sampling="lhs",
    )
    return replace(base, **kwargs) if kwargs else base


def test_fmu_residual_anchors_official_at_p40() -> None:
    samples = mbal.build_sample_table(fmu_cfg())

    for key, official in (("A", 4.5), ("B", 3.0)):
        p40 = float(np.percentile(samples[f"stoiip_{key}"], 60))
        assert p40 == pytest.approx(official, rel=0.03)

    # Mapped volumes sit above the median — not treated as P50 or the mean.
    assert float(np.percentile(samples["stoiip_A"], 50)) < 4.5
    assert float(np.percentile(samples["stoiip_B"], 50)) < 3.0
    assert float(samples["stoiip_total"].mean()) <= 7.5 * 1.01


def test_shared_field_scale_correlates_tanks() -> None:
    samples = mbal.build_sample_table(fmu_cfg(n_realizations=3_000, seed=3))

    corr = float(np.corrcoef(samples["stoiip_A"], samples["stoiip_B"])[0, 1])
    assert corr > 0.45
    np.testing.assert_allclose(
        samples["stoiip_total"], samples["stoiip_A"] + samples["stoiip_B"]
    )
    np.testing.assert_allclose(samples["official_A"], 4.5)
    np.testing.assert_allclose(samples["official_B"], 3.0)


def test_independent_tanks_make_field_p90_optimistic() -> None:
    fmu = mbal.build_sample_table(fmu_cfg(n_realizations=5_000, seed=5))

    # Match each tank's fmu marginal with an independent lognormal, then sum.
    def matched(key: str) -> mbal.Distribution:
        values = fmu[f"stoiip_{key}"]
        return mbal.Distribution(
            kind="lognormal",
            p90=float(np.percentile(values, 10)),
            p10=float(np.percentile(values, 90)),
        )

    independent = mbal.build_sample_table(
        mbal.Config(
            tanks=(
                tank("A", 0, stoiip=matched("A")),
                tank("B", 1, stoiip=matched("B")),
            ),
            n_realizations=5_000,
            seed=5,
            sampling="lhs",
        )
    )

    fmu_p90 = float(np.percentile(fmu["stoiip_total"], 10))
    ind_p90 = float(np.percentile(independent["stoiip_total"], 10))
    # False diversification lifts the independent field low-case.
    assert fmu_p90 < ind_p90


def test_max_multiplier_caps_upside() -> None:
    cfg = fmu_cfg(
        volume_model=replace(fmu_cfg().volume_model, max_multiplier=1.05),
        n_realizations=800,
        seed=2,
    )
    samples = mbal.build_sample_table(cfg)
    assert samples["stoiip_A"].max() <= 4.5 * 1.05 + 1e-9
    assert samples["stoiip_B"].max() <= 3.0 * 1.05 + 1e-9


def test_fmu_rejects_stoiip_and_requires_official() -> None:
    cfg = fmu_cfg(
        tanks=(
            tank(
                "A",
                0,
                official=4.5,
                stoiip=mbal.Distribution(kind="fixed", value=4.5),
            ),
        )
    )
    with pytest.raises(ValueError, match="use official_stoiip"):
        mbal.validate_config(cfg)

    cfg = fmu_cfg(tanks=(tank("A", 0, stoiip=mbal.Distribution(kind="fixed", value=4.5)),))
    with pytest.raises(ValueError, match="official_stoiip"):
        mbal.validate_config(cfg)


def test_yaml_fmu_volume_model(tmp_path) -> None:
    path = tmp_path / "vol.yaml"
    path.write_text(
        """
volume_model:
  kind: fmu_residual
  official_as: p40
  field_scale: {kind: lognormal, p90: 0.7, p10: 1.18}
  residual: {kind: lognormal, p90: 0.85, p10: 1.12}
tanks:
  - {key: A, name: A, index: 0, official_stoiip: 4.5}
  - {key: B, name: B, index: 1, official_stoiip: 3.0}
""",
        encoding="utf-8",
    )
    cfg = mbal.load_config_yaml(path)
    mbal.validate_config(cfg)
    assert cfg.volume_model.kind == "fmu_residual"
    samples = mbal.build_sample_table(replace(cfg, n_realizations=20, seed=1))
    assert "field_scale" in samples.columns
    assert set(samples["official_A"].unique()) == {4.5}


def test_default_config_always_has_the_three_tanks() -> None:
    cfg = mbal.default_config()
    assert [tank.key for tank in cfg.tanks] == ["A", "B", "C"]
    assert [tank.official_stoiip for tank in cfg.tanks] == [4.5, 3.0, 6.5]
    assert cfg.tanks[2].role == "upside"
    assert cfg.tanks[2].in_model is False
    assert cfg.volume_model.kind == "connected_volume"
    assert cfg.gas_lift_values == ()
    assert cfg.water_inj_rate_values == ()


def connected_cfg(**kwargs) -> mbal.Config:
    base = mbal.default_config()
    base = replace(
        base,
        water_inj_rate_values=(),
        water_inj_bhp_values=(),
        n_realizations=4_000,
        seed=11,
        sampling="lhs",
    )
    return replace(base, **kwargs) if kwargs else base


def test_connected_volume_does_not_put_official_in_the_base_p50() -> None:
    samples = mbal.build_sample_table(connected_cfg())

    assert float(np.percentile(samples["stoiip_A"], 50)) < 4.5 * 0.85
    assert float(np.percentile(samples["stoiip_B"], 50)) < 3.0
    # Deeper sand is an optional upside: median is zero.
    assert float(np.percentile(samples["stoiip_C"], 50)) == pytest.approx(0.0)
    assert float(np.percentile(samples["stoiip_base"], 50)) < 7.5
    # Mean includes the upside tail; P50 of total should stay near the base.
    assert float(np.percentile(samples["stoiip_total"], 50)) < 8.0
    assert samples["stoiip_A"].mean() < 4.5
    assert samples["stoiip_C"].mean() < 6.5 * 0.5


def test_connected_volume_two_section_uses_half_when_isolated() -> None:
    cfg = connected_cfg(
        n_realizations=2_000,
        seed=8,
        tanks=(
            mbal.TankConfig(
                key="A",
                name="A",
                index=0,
                official_stoiip=4.5,
                connectivity=mbal.Connectivity(
                    kind="two_section",
                    p_connected=0.0,
                    isolated_fraction=0.50,
                    residual=mbal.Distribution(kind="fixed", value=1.0),
                ),
            ),
        ),
        volume_model=mbal.VolumeModel(kind="connected_volume"),
    )
    samples = mbal.build_sample_table(cfg)
    np.testing.assert_allclose(samples["stoiip_A"], 2.25)
    np.testing.assert_allclose(samples["connected_A"], 0.0)


def test_optional_upside_is_off_or_full() -> None:
    cfg = connected_cfg(
        n_realizations=3_000,
        seed=1,
        tanks=(
            mbal.TankConfig(
                key="C",
                name="C",
                index=2,
                official_stoiip=6.5,
                role="upside",
                in_model=False,
                connectivity=mbal.Connectivity(
                    kind="optional",
                    p_connected=0.25,
                    isolated_fraction=0.0,
                    residual=mbal.Distribution(kind="fixed", value=1.0),
                ),
            ),
        ),
        volume_model=mbal.VolumeModel(kind="connected_volume"),
    )
    samples = mbal.build_sample_table(cfg)
    values = set(np.round(samples["stoiip_C"], 6))
    assert values <= {0.0, 6.5}
    assert 0.18 < float(samples["connected_C"].mean()) < 0.32


def test_apply_skips_tanks_not_in_the_mbal_model() -> None:
    cfg = connected_cfg(
        n_realizations=1,
        tanks=(
            mbal.TankConfig(
                key="A",
                name="TANK_A",
                index=0,
                official_stoiip=4.5,
                connectivity=mbal.Connectivity(
                    kind="two_section",
                    p_connected=1.0,
                    isolated_fraction=0.5,
                    residual=mbal.Distribution(kind="fixed", value=1.0),
                ),
            ),
            mbal.TankConfig(
                key="C",
                name="TANK_C",
                index=2,
                official_stoiip=6.5,
                role="upside",
                in_model=False,
                connectivity=mbal.Connectivity(
                    kind="optional",
                    p_connected=1.0,
                    residual=mbal.Distribution(kind="fixed", value=1.0),
                ),
            ),
        ),
        volume_model=mbal.VolumeModel(kind="connected_volume"),
    )

    class RecordingServer:
        def __init__(self) -> None:
            self.values: list[tuple[str, float | str]] = []

        def set(self, tag: str, value: float | str) -> None:
            self.values.append((tag, value))

    row = pd.Series({"stoiip_A": 4.5, "stoiip_C": 6.5})
    server = RecordingServer()
    mbal.apply_realization(server, row, cfg)
    tags = [tag for tag, _value in server.values]
    assert any("TANK_A" in tag for tag in tags)
    assert not any("TANK_C" in tag for tag in tags)


def test_volume_model_roundtrip_in_example_config(tmp_path) -> None:
    path = tmp_path / "example.yaml"
    assert core.main(["--write-example-config", str(path)]) == 0
    cfg = mbal.load_config_yaml(path, base=mbal.default_config())
    mbal.validate_config(cfg)
    assert cfg.volume_model.kind == "connected_volume"
    assert cfg.tanks[0].official_stoiip == 4.5
    assert cfg.tanks[1].official_stoiip == 3.0
    assert cfg.tanks[2].official_stoiip == 6.5
    assert cfg.tanks[2].role == "upside"
    assert cfg.tanks[2].in_model is False


def correlated_cfg(correlation: float, **kwargs) -> mbal.Config:
    """Default three tanks with the base-sand group at a given correlation."""
    base = connected_cfg(n_realizations=20_000, seed=7)
    return replace(
        base,
        volume_model=replace(
            base.volume_model, connectivity_correlation=correlation
        ),
        **kwargs,
    )


def test_shared_connectivity_group_couples_the_isolation_risk() -> None:
    """One barrier decides both base sands, so the draws cannot diversify."""

    def joint(correlation: float) -> tuple[float, float]:
        samples = mbal.build_sample_table(correlated_cfg(correlation))
        connected_a = samples["connected_A"] > 0.5
        connected_b = samples["connected_B"] > 0.5
        return (
            float((connected_a & connected_b).mean()),
            float((~connected_a & ~connected_b).mean()),
        )

    both_independent, neither_independent = joint(0.0)
    both_shared, neither_shared = joint(1.0)

    # Independent draws multiply; one shared barrier gives the comonotonic
    # bound: P(both) = min(p), P(neither) = 1 - max(p).
    assert both_independent == pytest.approx(0.30 * 0.35, abs=0.02)
    assert neither_independent == pytest.approx(0.70 * 0.65, abs=0.02)
    assert both_shared == pytest.approx(0.30, abs=0.02)
    assert neither_shared == pytest.approx(0.65, abs=0.02)

    # The decision statement the well cares about moves a long way.
    assert both_shared > 2 * both_independent
    assert neither_shared > neither_independent


def test_connectivity_correlation_leaves_each_tank_marginal_alone() -> None:
    """Coupling changes the joint behaviour only — never a tank's own odds."""
    for correlation in (0.0, 0.5, 0.8, 1.0):
        samples = mbal.build_sample_table(correlated_cfg(correlation))
        assert float(samples["connected_A"].mean()) == pytest.approx(0.30, abs=0.02)
        assert float(samples["connected_B"].mean()) == pytest.approx(0.35, abs=0.02)
        assert float(samples["stoiip_A"].mean()) == pytest.approx(2.85, rel=0.05)


def test_upside_sand_is_not_dragged_along_by_the_base_barrier() -> None:
    """C is a presence/charge question, not the same fault as A and B."""
    samples = mbal.build_sample_table(correlated_cfg(1.0))
    connected_a = samples["connected_A"] > 0.5
    connected_c = samples["connected_C"] > 0.5
    joint = float((connected_a & connected_c).mean())
    product = float(connected_a.mean()) * float(connected_c.mean())
    assert joint == pytest.approx(product, abs=0.02)


def test_ungrouped_connectivity_ignores_the_correlation_knob() -> None:
    """Existing configs and seeds must reproduce bit-for-bit."""
    base = connected_cfg(n_realizations=2_000, seed=3)
    ungrouped = tuple(
        replace(item, connectivity=replace(item.connectivity, group=None))
        for item in base.tanks
    )
    off = replace(
        base,
        tanks=ungrouped,
        volume_model=replace(base.volume_model, connectivity_correlation=0.0),
    )
    on = replace(
        base,
        tanks=ungrouped,
        volume_model=replace(base.volume_model, connectivity_correlation=0.9),
    )
    assert mbal.build_sample_table(off).equals(mbal.build_sample_table(on))


def test_correlation_outside_zero_to_one_is_rejected() -> None:
    for bad in (-0.1, 1.5):
        cfg = correlated_cfg(bad, n_realizations=10)
        with pytest.raises(ValueError, match="connectivity_correlation"):
            mbal.validate_config(cfg)


def test_grouped_tanks_without_correlation_are_warned_about(caplog) -> None:
    cfg = correlated_cfg(0.0, n_realizations=10)
    with caplog.at_level("WARNING", logger="mbal"):
        mbal.validate_config(cfg)
    assert any("false diversification" in message for message in caplog.messages)


def test_yaml_connectivity_group_and_correlation(tmp_path) -> None:
    path = tmp_path / "conn.yaml"
    path.write_text(
        """
volume_model:
  kind: connected_volume
  connectivity_correlation: 1.0
tanks:
  - key: A
    name: A
    index: 0
    official_stoiip: 4.5
    connectivity:
      kind: two_section
      p_connected: 0.3
      isolated_fraction: 0.5
      group: main_fault
      residual: {kind: fixed, value: 1.0}
  - key: B
    name: B
    index: 1
    official_stoiip: 3.0
    connectivity:
      kind: two_section
      p_connected: 0.3
      isolated_fraction: 0.5
      group: main_fault
      residual: {kind: fixed, value: 1.0}
""",
        encoding="utf-8",
    )
    cfg = mbal.load_config_yaml(path)
    mbal.validate_config(cfg)
    assert cfg.tanks[0].connectivity.group == "main_fault"
    assert cfg.volume_model.connectivity_correlation == 1.0

    # Equal p_connected on one shared barrier: the tanks move together.
    samples = mbal.build_sample_table(replace(cfg, n_realizations=1_000, seed=4))
    np.testing.assert_allclose(samples["connected_A"], samples["connected_B"])
