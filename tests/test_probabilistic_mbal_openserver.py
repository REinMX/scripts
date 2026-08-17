"""Tests for per-tank probabilistic MBAL sampling and the run plumbing."""

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
    official: float,
    aquifer: mbal.Distribution | None = None,
) -> mbal.TankConfig:
    """A fixed-volume tank for OpenServer plumbing tests."""
    return mbal.TankConfig(
        key=key,
        name=f"Tank {key}",
        index=index,
        official_stoiip=official,
        aquifer_multiplier=aquifer,
    )


def index_cfg(**kwargs) -> mbal.Config:
    """Config pinned to index-based OpenServer tags."""
    tags = kwargs.pop("tags", dict(mbal.DEFAULT_INDEX_TAGS))
    return mbal.Config(
        tag_mode="index",
        tags=tags,
        unit_stoiip="MMstb",
        unit_press="psig",
        unit_cum="MMstb",
        **kwargs,
    )


def test_arbitrary_number_of_tanks_is_supported() -> None:
    cfg = mbal.Config(
        tanks=(
            tank("North", 2, 12.0),
            tank("Central", 5, 25.0),
            tank("South", 7, 40.0),
        ),
        n_realizations=25,
        seed=9,
    )

    samples = mbal.build_sample_table(cfg)

    stoiip_columns = ["stoiip_North", "stoiip_Central", "stoiip_South"]
    assert set(stoiip_columns).issubset(samples.columns)
    np.testing.assert_allclose(samples["stoiip_North"], 12.0)
    np.testing.assert_allclose(samples["stoiip_South"], 40.0)
    np.testing.assert_allclose(
        samples["stoiip_total"], samples[stoiip_columns].sum(axis=1)
    )


def test_aquifer_multiplier_is_configured_and_sampled_per_tank() -> None:
    tags = dict(mbal.DEFAULT_INDEX_TAGS)
    tags["aquifer_mult"] = "MBAL.MB[0].TANK[{i}].CUSTOM_AQUIFER_MULTIPLIER"
    cfg = index_cfg(
        tanks=(
            tank("A", 0, 30.0, uniform(0.5, 1.5)),
            tank("B", 1, 50.0, fixed(2.0)),
        ),
        n_realizations=100,
        seed=3,
        tags=tags,
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
    tags = dict(mbal.DEFAULT_INDEX_TAGS)
    tags["aquifer_mult"] = "MBAL.MB[0].TANK[{i}].CUSTOM_AQUIFER_MULTIPLIER"
    cfg = index_cfg(
        tanks=(
            tank("East", 3, 20.0, fixed(1.2)),
            tank("West", 8, 70.0),
        ),
        tags=tags,
    )
    row = pd.Series({"stoiip_East": 21.0, "aq_mult_East": 1.3, "stoiip_West": 73.0})
    server = RecordingServer()

    mbal.apply_realization(server, row, cfg)

    assert server.values == [
        ("MBAL.MB[0].TANK[3].OOIP", 21.0),
        ("MBAL.MB[0].TANK[3].CUSTOM_AQUIFER_MULTIPLIER", 1.3),
        ("MBAL.MB[0].TANK[8].OOIP", 73.0),
    ]


def test_default_tags_use_the_supported_mbal_openserver_hierarchy() -> None:
    index_tags = mbal.DEFAULT_INDEX_TAGS
    name_tags = mbal.DEFAULT_NAME_TAGS

    assert index_tags["tank_stoiip"] == "MBAL.MB[0].TANK[{i}].OOIP"
    assert index_tags["aquifer_volume"] == "MBAL.MB[0].TANK[{i}].AQUIF.VOLUME"
    assert (
        index_tags["gas_lift_rate"]
        == "MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MAX_GASLIFT"
    )
    assert (
        index_tags["water_inj_rate"]
        == "MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MAXINJWATRATE"
    )
    assert (
        index_tags["water_inj_min_rate"]
        == "MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MININJWATRATE"
    )
    assert index_tags["cmd_run_pred"] == "MBAL.MB.RunPrediction"
    assert index_tags["res_nsteps"] == "MBAL.MB[0].TRES[2][{r}].COUNT"
    assert "PREDICTION.RESULTS" not in " ".join(index_tags.values())

    cfg = mbal.Config(
        tanks=(tank("A", 0, 10.0),),
        tags=dict(name_tags),
        tag_mode="name",
    )
    assert core._stoiip_tag(cfg, cfg.tanks[0]) == "MBAL.MB[0].TANK[{Tank A}].OOIP"


def test_new_result_record_has_stable_columns_before_a_failed_run() -> None:
    cfg = mbal.Config(tanks=(tank("East", 3, 20.0), tank("West", 8, 70.0)))
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
    cfg = mbal.Config(tanks=(tank("A", 0, 10.0), tank("A", 1, 20.0)))

    with pytest.raises(ValueError, match="tank keys must be unique"):
        mbal.build_sample_table(cfg)


def test_percentiles_default_to_normal_oil_and_gas_cases_only() -> None:
    stats = mbal.percentiles(np.array([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert set(stats) == {"P90", "P50", "P10", "mean", "std"}
    assert stats["P50"] == pytest.approx(3.0)
    assert stats["std"] == pytest.approx(np.std([1, 2, 3, 4, 5]))


def test_resume_retries_failed_but_skips_ok(tmp_path) -> None:
    cfg = mbal.Config(
        tanks=(tank("A", 0, 10.0), tank("B", 1, 20.0)),
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
    assert [tank.result_index for tank in cfg.tanks] == [1, 2, 3]
    assert cfg.tag_mode == "name"
    assert [tank.p90_stoiip for tank in cfg.tanks] == [3.5, None, 5.0]
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


def test_unreadable_step_count_fails_instead_of_reading_results_index_zero() -> None:
    """COUNT is required: row zero is the first prediction step, not the last."""
    cfg = mbal.Config(tanks=(tank("A", 0, 10.0),))

    class StubServer:
        def __init__(self) -> None:
            self.reads: list[str] = []

        def get(self, tag: str) -> float:
            self.reads.append(tag)
            if "COUNT" in tag:
                raise RuntimeError("OpenServer error 1 on DoGet")
            return 1.0

    server = StubServer()
    row = pd.Series({"stoiip_A": 10.0})

    with pytest.raises(RuntimeError, match="COUNT"):
        mbal.read_results(server, cfg, row)
    assert all("[0]" not in read.split(".COUNT")[0][-3:] for read in server.reads)


def test_readable_step_count_reads_the_last_step_without_warning(caplog) -> None:
    configured_tank = replace(tank("A", 0, 10.0), result_index=7)
    cfg = index_cfg(tanks=(configured_tank,))

    class StubServer:
        def __init__(self) -> None:
            self.reads: list[str] = []

        def get(self, tag: str) -> float:
            self.reads.append(tag)
            return 25.0 if "COUNT" in tag else 1.0

    server = StubServer()
    with caplog.at_level("WARNING", logger="mbal"):
        mbal.read_results(server, cfg, pd.Series({"stoiip_A": 10.0}))
    assert not caplog.messages
    assert "MBAL.MB[0].TRES[2][7].COUNT" in server.reads
    assert any("[2][7][24]" in tag for tag in server.reads)


def test_single_tank_defaults_to_tres_sheet_zero() -> None:
    configured_tank = tank("A", 0, 10.0)
    cfg = index_cfg(tanks=(configured_tank,))

    assert core._format_result_tag(cfg, "res_nsteps", configured_tank) == (
        "MBAL.MB[0].TRES[2][0].COUNT"
    )
    mbal.validate_config(replace(cfg, tanks=(replace(configured_tank, result_index=0),)))


def test_multi_tank_rejects_consolidated_or_duplicate_result_sheets() -> None:
    tank_a = replace(tank("A", 0, 10.0), result_index=0)
    tank_b = replace(tank("B", 1, 10.0), result_index=1)

    with pytest.raises(ValueError, match="sheet 0 is consolidated"):
        mbal.validate_config(index_cfg(tanks=(tank_a, tank_b)))

    duplicate_a = replace(tank_a, result_index=1)
    with pytest.raises(ValueError, match="duplicate result_index"):
        mbal.validate_config(index_cfg(tanks=(duplicate_a, tank_b)))


def test_gas_lift_tag_uses_the_prediction_constraint_index() -> None:
    cfg = mbal.Config(
        tag_mode="index",
        tags=dict(mbal.DEFAULT_INDEX_TAGS),
        gas_lift_well="INJ-2",
        gas_lift_well_index=3,
        gas_lift_prediction_index=1,
        gas_lift_values=(0.0, 1.0),
    )
    assert (
        core._gas_lift_tag(cfg)
        == "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT"
    )

    named = replace(
        cfg, tag_mode="name", tags=dict(mbal.DEFAULT_NAME_TAGS)
    )
    assert (
        core._gas_lift_tag(named)
        == "MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT"
    )


def test_aquifer_distribution_without_its_tag_is_rejected_up_front() -> None:
    """Fail at config time, not once per realization inside the run loop."""
    # Name-mode defaults ship aquifer_volume but not aquifer_mult.
    cfg = mbal.Config(tanks=(tank("A", 0, 10.0, aquifer=fixed(1.0)),))
    with pytest.raises(ValueError, match="aquifer_mult"):
        mbal.validate_config(cfg)

    # Both modes ship the supported TANK.AQUIF.VOLUME tag.
    with_volume = replace(
        tank("A", 0, 10.0),
        aquifer_multiplier=None,
        aquifer_volume=fixed(1.0),
    )
    mbal.validate_config(index_cfg(tanks=(with_volume,)))


def test_openserver_rejects_strings_above_the_transfer_limit() -> None:
    core._validate_openserver_string("x" * 65_500, "test")
    with pytest.raises(ValueError, match="65500-character"):
        core._validate_openserver_string("x" * 65_501, "test")


def test_com_wrapper_uses_documented_direct_openserver_methods() -> None:
    class FakeCom:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def DoCommand(self, command: str) -> int:
            self.calls.append(("DoCommand", command))
            return 0

        def SetValue(self, tag: str, value: float | str) -> int:
            self.calls.append(("SetValue", tag, value))
            return 0

        def GetValue(self, tag: str) -> str:
            self.calls.append(("GetValue", tag))
            return "12.5"

        def GetLastError(self, app: str) -> int:
            self.calls.append(("GetLastError", app))
            return 0

        def GetErrorDescription(self, error: int) -> str:
            return f"error {error}"

    com = FakeCom()
    server = object.__new__(core.OpenServer)
    server.os = com

    server.cmd("MBAL.START")
    server.slow_cmd("MBAL.MB.RunPrediction")
    server.set("MBAL.MB[0].TANK[0].OOIP", 12.5)
    assert server.get("MBAL.MB[0].TANK[0].OOIP") == 12.5
    assert com.calls == [
        ("DoCommand", "MBAL.START"),
        ("DoCommand", "MBAL.MB.RunPrediction"),
        ("SetValue", "MBAL.MB[0].TANK[0].OOIP", 12.5),
        ("GetValue", "MBAL.MB[0].TANK[0].OOIP"),
        ("GetLastError", "MBAL"),
    ]


def test_summarize_only_without_results_reports_instead_of_tracebacking(
    tmp_path, capsys
) -> None:
    code = core.main(
        ["--summarize-only", "--out-dir", str(tmp_path / "nothing-here")]
    )
    assert code == 1
    assert "No results CSV" in capsys.readouterr().err


def test_required_prediction_result_tags_are_validated_up_front() -> None:
    tags = dict(mbal.DEFAULT_NAME_TAGS)
    del tags["res_cumoil"]
    cfg = replace(mbal.default_config(), tags=tags)

    with pytest.raises(ValueError, match="res_cumoil"):
        mbal.validate_config(cfg)


def test_licensed_readiness_rejects_example_placeholders() -> None:
    with pytest.raises(ValueError, match="example/placeholder"):
        mbal.validate_licensed_run_config(mbal.default_config())


def test_validate_config_cli_is_offline_and_writes_no_output(tmp_path, capsys) -> None:
    yaml = pytest.importorskip("yaml")
    cfg = replace(
        mbal.default_config(),
        mbal_file=r"C:\\Private\\Models\\working_copy.mbi",
        tanks=tuple(
            replace(item, name=f"PRIVATE_TANK_{item.key}")
            for item in mbal.default_config().tanks
        ),
        out_dir=str(tmp_path / "must-not-exist"),
    )
    path = tmp_path / "mbal_config.local.yaml"
    path.write_text(
        yaml.safe_dump(mbal.config_to_dict(cfg), sort_keys=False), encoding="utf-8"
    )

    assert mbal.main(["--config", str(path), "--validate-config"]) == 0
    assert not (tmp_path / "must-not-exist").exists()
    output = capsys.readouterr().out
    assert "No MBAL/OpenServer session was opened" in output
    assert "PRIVATE_TANK" not in output


def test_check_openserver_is_windows_only_without_dispatch(tmp_path, capsys) -> None:
    yaml = pytest.importorskip("yaml")
    cfg = replace(
        mbal.default_config(),
        mbal_file=r"C:\\Private\\Models\\working_copy.mbi",
        tanks=tuple(
            replace(item, name=f"PRIVATE_TANK_{item.key}")
            for item in mbal.default_config().tanks
        ),
        out_dir=str(tmp_path / "must-not-exist"),
    )
    path = tmp_path / "mbal_config.local.yaml"
    path.write_text(
        yaml.safe_dump(mbal.config_to_dict(cfg), sort_keys=False), encoding="utf-8"
    )

    assert mbal.main(["--config", str(path), "--check-openserver"]) == 1
    assert "Windows-only" in capsys.readouterr().err
    assert not (tmp_path / "must-not-exist").exists()
