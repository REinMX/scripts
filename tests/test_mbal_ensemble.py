"""Tests for volume sampling and the ensemble runner built on mbal_simple."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

import mbal_ensemble as ensemble
import mbal_simple as ms


def tank(
    name: str,
    mean: float,
    *,
    p90: float | None = None,
    p10: float | None = None,
) -> ms.Tank:
    return ms.Tank(
        name=name,
        inputs={"stoiip": mean},
        p90_stoiip=p90,
        p10_stoiip=p10,
    )


def test_fixed_tank_stays_at_its_official_mean() -> None:
    cfg = ms.Config(
        mbal_file="model.mbi",
        tanks=(tank("Fixed", 4.5),),
        n_realizations=25,
        seed=7,
    )

    samples = ensemble.build_sample_table(cfg)

    assert list(samples.columns) == ["realization", "Fixed.OOIP", "stoiip_total"]
    np.testing.assert_allclose(samples["Fixed.OOIP"], 4.5)
    np.testing.assert_allclose(samples["stoiip_total"], 4.5)


def test_uncertain_tank_matches_p90_mean_and_p10() -> None:
    cfg = ms.Config(
        mbal_file="model.mbi",
        tanks=(tank("Uncertain", 10.0, p90=7.0, p10=14.0),),
        n_realizations=20_000,
        seed=17,
        sampling="lhs",
    )

    values = ensemble.build_sample_table(cfg)["Uncertain.OOIP"].to_numpy()

    assert np.percentile(values, 10) == pytest.approx(7.0, rel=0.01)
    assert values.mean() == pytest.approx(10.0, rel=0.005)
    assert np.percentile(values, 90) == pytest.approx(14.0, rel=0.01)


def test_tanks_use_independent_dimensions_and_field_is_the_row_sum() -> None:
    cfg = ms.Config(
        mbal_file="model.mbi",
        tanks=(
            tank("Top", 10.0, p90=7.0, p10=14.0),
            tank("Middle", 5.0),
            tank("Bottom", 20.0, p90=14.0, p10=28.0),
        ),
        n_realizations=10_000,
        seed=3,
        sampling="lhs",
    )

    samples = ensemble.build_sample_table(cfg)

    np.testing.assert_allclose(
        samples["stoiip_total"],
        samples[["Top.OOIP", "Middle.OOIP", "Bottom.OOIP"]].sum(axis=1),
    )
    assert samples["Middle.OOIP"].eq(5.0).all()
    rank_correlation = samples[["Top.OOIP", "Bottom.OOIP"]].rank().corr()
    assert abs(rank_correlation.iloc[0, 1]) < 0.04


def test_one_matching_prior_is_recorded_and_reproduces_the_mean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = batch_config(tmp_path)

    samples = ensemble.build_sample_table(cfg)
    assert "matched by more than one prior" not in capsys.readouterr().err
    summary = ensemble.summarize(samples, cfg).set_index("variable")

    median = summary.at["Top.OOIP", "fitted_median"]
    assert summary.at["Top.OOIP", "fitted_rivals"] == 0
    assert ensemble._split_lognormal_mean(median, 7.0, 14.0)[0] == pytest.approx(10.0)
    assert median < 10.0  # right-skewed, so the median sits below the mean
    assert np.isnan(summary.at["Bottom.OOIP", "fitted_median"])
    assert np.isnan(summary.at["stoiip_total", "fitted_median"])


def test_rival_priors_are_reported_and_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A P10/P90 of 10 makes the mean non-monotone in the median, so two
    # different distributions reproduce these same three statistics.
    cfg = ms.Config(
        mbal_file="model.mbi",
        tanks=(tank("Wide", 4.704, p90=1.0, p10=10.0),),
        out_dir=str(tmp_path),
        n_realizations=50,
        seed=5,
    )

    samples = ensemble.build_sample_table(cfg)

    warning = capsys.readouterr().err
    assert "tank Wide" in warning
    assert "matched by more than one prior" in warning
    summary = ensemble.summarize(samples, cfg).set_index("variable")
    assert summary.at["Wide.OOIP", "fitted_rivals"] == 1
    assert summary.at["Wide.OOIP", "fitted_median"] == pytest.approx(3.0147, rel=1e-3)
    assert samples["Wide.OOIP"].mean() == pytest.approx(4.704, rel=0.05)


def test_incompatible_mean_and_quantiles_fail_clearly() -> None:
    cfg = ms.Config(
        mbal_file="model.mbi",
        tanks=(tank("Wide", 10.0, p90=1.0, p10=100.0),),
        n_realizations=20,
    )

    with pytest.raises(ValueError, match="cannot match its mean, P90 and P10"):
        ensemble.build_sample_table(cfg)


class FakeMBAL:
    """Small field-result MBAL double using the verified simple tag shapes."""

    def __init__(
        self,
        tank_names: tuple[str, ...],
        *,
        fail_run: int | None = None,
        nan_result: bool = False,
    ):
        self.saved = {name: 1.0 for name in tank_names}
        self.store: dict[str, float | str] = {}
        self.commands: list[str] = []
        self.writes: list[tuple[str, float | str]] = []
        self.run_count = 0
        self.fail_run = fail_run
        self.nan_result = nan_result
        self.failed_once = False
        self.predicted = False
        self.open()

    def open(self) -> None:
        self.store = {f"ooip::{name}": value for name, value in self.saved.items()}
        self.predicted = False

    def cmd(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("MBAL.OPENFILE"):
            self.open()
            return
        if command == ms.RUN_TAG:
            self.run_count += 1
            if self.run_count == self.fail_run and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("synthetic prediction failure")
            self.predicted = True
            return
        raise RuntimeError(f"unknown command {command}")

    def set(self, tag: str, value: float | str) -> None:
        self.writes.append((tag, value))
        self.store[self._key(tag)] = value

    def get(self, tag: str) -> float:
        return float(self.get_raw(tag))

    def get_raw(self, tag: str) -> float | str:
        key = self._key(tag)
        if key == "count":
            if not self.predicted:
                raise RuntimeError("prediction has not run")
            return 2.0
        if key == "cum_oil":
            if not self.predicted:
                raise RuntimeError("prediction has not run")
            if self.nan_result:
                return float("nan")
            return 0.1 * sum(
                float(value)
                for name, value in self.store.items()
                if name.startswith("ooip::")
            )
        if key in self.store:
            return self.store[key]
        raise RuntimeError(f"unreadable tag {tag}")

    @staticmethod
    def _key(tag: str) -> str:
        tank_match = re.fullmatch(
            r"MBAL\.MB\[0\]\.TANK\[\{(.+?)\}\]\.OOIP", tag
        )
        if tank_match:
            return f"ooip::{tank_match.group(1)}"
        if tag.endswith(".COUNT"):
            return "count"
        if tag.endswith(".CUMOIL"):
            return "cum_oil"
        return tag


def batch_config(tmp_path: Path, *, n: int = 3) -> ms.Config:
    return ms.Config(
        mbal_file="model.mbi",
        tanks=(
            tank("Top", 10.0, p90=7.0, p10=14.0),
            tank("Bottom", 5.0),
        ),
        results=ms.Results(read={"cum_oil": "CUMOIL"}),
        out_dir=str(tmp_path),
        n_realizations=n,
        seed=11,
        sampling="lhs",
    )


def test_ensemble_runs_every_sample_through_the_simple_coupling(tmp_path: Path) -> None:
    cfg = batch_config(tmp_path)
    samples = ensemble.build_sample_table(cfg)
    server = FakeMBAL(("Top", "Bottom"))

    results = ensemble.run_ensemble(samples, cfg, server)

    assert results["status"].tolist() == ["ok", "ok", "ok"]
    assert server.run_count == 3
    volume_writes = [tag for tag, _value in server.writes if tag.endswith(".OOIP")]
    assert len(volume_writes) == 6
    np.testing.assert_allclose(results["cum_oil"], results["stoiip_total"] * 0.1)
    assert server.commands[-1].startswith("MBAL.OPENFILE")
    assert server.store["ooip::Top"] == 1.0
    assert server.store["ooip::Bottom"] == 1.0
    written = pd.read_csv(tmp_path / "ensemble_results.csv")
    assert written["realization"].tolist() == [0, 1, 2]


def test_ensemble_resume_skips_ok_rows_and_retries_failures(tmp_path: Path) -> None:
    cfg = batch_config(tmp_path)
    samples = ensemble.build_sample_table(cfg)
    first = ensemble.run_ensemble(
        samples, cfg, FakeMBAL(("Top", "Bottom"), fail_run=2)
    )
    assert first["status"].tolist() == ["ok", "failed", "ok"]

    retry_server = FakeMBAL(("Top", "Bottom"))
    retried = ensemble.run_ensemble(samples, cfg, retry_server)

    assert retried["status"].tolist() == ["ok", "ok", "ok"]
    assert retry_server.run_count == 1


def test_nonfinite_required_result_is_recorded_as_failed(tmp_path: Path) -> None:
    cfg = batch_config(tmp_path, n=1)

    results = ensemble.run_ensemble(
        ensemble.build_sample_table(cfg),
        cfg,
        FakeMBAL(("Top", "Bottom"), nan_result=True),
    )

    assert results.at[0, "status"] == "failed"
    assert "cum_oil" in results.at[0, "error"]


def test_resume_retries_ok_row_with_missing_required_result(tmp_path: Path) -> None:
    cfg = batch_config(tmp_path)
    samples = ensemble.build_sample_table(cfg)
    ensemble.run_ensemble(samples, cfg, FakeMBAL(("Top", "Bottom")))
    path = tmp_path / "ensemble_results.csv"
    stored = pd.read_csv(path)
    stored.loc[1, "cum_oil"] = float("nan")
    stored.to_csv(path, index=False)
    server = FakeMBAL(("Top", "Bottom"))

    results = ensemble.run_ensemble(samples, cfg, server)

    assert results["status"].tolist() == ["ok", "ok", "ok"]
    assert server.run_count == 1


def test_ensemble_resume_rejects_changed_samples_before_opening_mbal(
    tmp_path: Path,
) -> None:
    cfg = batch_config(tmp_path)
    samples = ensemble.build_sample_table(cfg)
    ensemble.run_ensemble(samples, cfg, FakeMBAL(("Top", "Bottom")))
    changed = samples.copy()
    changed.loc[0, "Top.OOIP"] += 1.0
    server = FakeMBAL(("Top", "Bottom"))
    server.commands.clear()

    with pytest.raises(RuntimeError, match="different Top.OOIP"):
        ensemble.run_ensemble(changed, cfg, server)

    assert server.commands == []


def test_ensemble_profile_keeps_every_step_for_every_realization(
    tmp_path: Path,
) -> None:
    cfg = batch_config(tmp_path, n=2)
    cfg = ms.Config(
        **{
            **cfg.__dict__,
            "results": ms.Results(read={"cum_oil": "CUMOIL"}, profile=True),
        }
    )

    ensemble.run_ensemble(
        ensemble.build_sample_table(cfg), cfg, FakeMBAL(("Top", "Bottom"))
    )

    profiles = pd.read_csv(tmp_path / "ensemble_profiles.csv")
    assert profiles[["realization", "step"]].values.tolist() == [
        [0, 0],
        [0, 1],
        [1, 0],
        [1, 1],
    ]


def write_yaml_config(tmp_path: Path, tanks: list[dict], **extra) -> Path:
    config_path = tmp_path / "case.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mbal_file": "model.mbi",
                "n_realizations": 25,
                "seed": 9,
                "sampling": "lhs",
                "out_dir": str(tmp_path / "out"),
                "tanks": tanks,
                **extra,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


UNCERTAIN_AND_FIXED = [
    {"name": "Top", "stoiip": 10.0, "p90_stoiip": 7.0, "p10_stoiip": 14.0},
    {"name": "Bottom", "stoiip": 5.0},
]
ALL_FIXED = [
    {"name": "Top", "stoiip": 10.0},
    {"name": "Bottom", "stoiip": 5.0},
]


def test_run_refuses_a_config_with_nothing_to_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_yaml_config(tmp_path, ALL_FIXED)

    assert ensemble.main([str(config_path), "--run"]) == 2

    assert "no tank" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_dry_run_warns_but_still_samples_a_fully_fixed_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_yaml_config(tmp_path, ALL_FIXED)

    assert ensemble.main([str(config_path), "--dry-run", "--n", "5"]) == 0

    assert "no tank" in capsys.readouterr().err
    samples = pd.read_csv(tmp_path / "out" / "ensemble_samples.csv")
    assert samples["stoiip_total"].eq(15.0).all()


def test_dry_run_cli_writes_n_samples_and_mean_anchored_summary(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    config_path = write_yaml_config(tmp_path, UNCERTAIN_AND_FIXED)

    assert ensemble.main([str(config_path), "--dry-run", "--n", "200"]) == 0

    samples = pd.read_csv(out_dir / "ensemble_samples.csv")
    assert len(samples) == 200
    summary = pd.read_csv(out_dir / "ensemble_summary.csv").set_index("variable")
    assert summary.at["Top.OOIP", "target_mean"] == 10.0
    assert summary.at["Top.OOIP", "target_P90"] == 7.0
    assert summary.at["Top.OOIP", "target_P10"] == 14.0
    assert summary.at["Bottom.OOIP", "sampled_mean"] == pytest.approx(5.0)
