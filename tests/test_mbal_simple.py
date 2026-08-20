"""Tests for the simple deterministic runner.

A fake OpenServer stands in for MBAL: it stores whatever is written and, on
RunPrediction, produces a field-level result stream derived from the OOIP it
currently holds. That is enough to prove the baseline writes nothing, that
--run writes the YAML values, and that --match sees a difference when there
is one.

The tag shapes here are the real ones from the model this was built for:
per-tank TANK[{name}].OOIP and one TRES[{Prediction}][{Prediction}] stream
whose trailing index is the time step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import mbal_simple as ms

TANKS = {"OS-top": 4.5, "OS-bottom": 3.0}
STREAM = "MBAL.MB[0].TRES[{Prediction}][{Prediction}]"
STEPS = 3


class FakeMBAL:
    """Minimal MBAL: tank inputs in, one prediction stream out."""

    def __init__(self, volumes: dict[str, float] | None = None):
        self.saved = dict(volumes or TANKS)
        self.store: dict[str, float | str] = {}
        self.commands: list[str] = []
        self.writes: list[tuple[str, float | str]] = []
        self.predicted = False
        self.open()

    def open(self) -> None:
        self.store = {f"ooip::{name}": value for name, value in self.saved.items()}
        self.predicted = False

    def cmd(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("MBAL.OPENFILE"):
            self.open()
        elif command == ms.RUN_TAG:
            self.predicted = True
        else:
            raise RuntimeError(f"unknown command {command}")

    def set(self, tag: str, value: float | str) -> None:
        self.writes.append((tag, value))
        key = self._key(tag)
        self.store[key] = value if isinstance(value, str) else float(value)

    def get_raw(self, tag: str) -> float | str:
        key = self._key(tag)
        if key == "count":
            self._require_prediction(tag)
            return float(STEPS)
        match = re.fullmatch(r"result::(\d+)::(\w+)", key)
        if match:
            self._require_prediction(tag)
            return self._result(int(match.group(1)), match.group(2))
        if key in self.store:
            return self.store[key]
        raise RuntimeError(f"unreadable tag {tag}")

    def get(self, tag: str) -> float:
        return float(self.get_raw(tag))

    def _result(self, step: int, variable: str) -> float | str:
        ooip = sum(
            float(value)
            for key, value in self.store.items()
            if key.startswith("ooip::")
        )
        if variable == "TIME":
            return f"2026-0{step + 1}-01"  # a date, not a number
        if variable == "CUMOIL":
            return ooip * 0.05 * (step + 1)
        if variable == "RESPRESS":
            return 3000.0 - 100.0 * (step + 1) * (7.5 / ooip)
        raise RuntimeError(f"unknown result variable {variable}")

    def _require_prediction(self, tag: str) -> None:
        if not self.predicted:
            raise RuntimeError(f"no prediction results for {tag}")

    @staticmethod
    def _key(tag: str) -> str:
        """Collapse a real access string into a simple store key."""
        tank = re.fullmatch(
            r"MBAL\.MB\[0\]\.TANK\[\{(.+?)\}\]\.([A-Z.]+)(\(.*\))?", tag
        )
        if tank:
            suffix = {"OOIP": "ooip", "AQUIF.VOLUME": "aquifer"}.get(
                tank.group(2), tank.group(2).lower()
            )
            return f"{suffix}::{tank.group(1)}"
        if tag == f"{STREAM}.COUNT":
            return "count"
        result = re.fullmatch(re.escape(STREAM) + r"\[(\d+)\]\.(\w+)", tag)
        if result:
            return f"result::{result.group(1)}::{result.group(2)}"
        return tag


def write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "mbal_file": r"C:\Work\model.mbi",
        "tanks": [{"name": name, "stoiip": value} for name, value in TANKS.items()],
        "results": {
            "stream": STREAM,
            "read": {"cum_oil": "CUMOIL", "res_pres": "RESPRESS"},
        },
        "out_dir": str(tmp_path / "out"),
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# --- config ---------------------------------------------------------------


def test_unknown_tank_key_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, tanks=[{"name": "OS-top", "stoip": 4.5}])
    with pytest.raises(ValueError, match="unknown key"):
        ms.load_config(path)


def test_missing_stoiip_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, tanks=[{"name": "OS-top"}])
    with pytest.raises(ValueError, match="stoiip is required"):
        ms.load_config(path)


def test_config_loads_ensemble_sampling_and_mean_volume_quantiles(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        n_realizations=25,
        seed=7,
        sampling="mc",
        tanks=[
            {
                "name": "OS-top",
                "stoiip": 10.0,
                "p90_stoiip": 7.0,
                "p10_stoiip": 14.0,
            }
        ],
    )

    cfg = ms.load_config(path)

    assert cfg.n_realizations == 25
    assert cfg.seed == 7
    assert cfg.sampling == "mc"
    assert cfg.tanks[0].inputs["stoiip"] == 10.0
    assert cfg.tanks[0].p90_stoiip == 7.0
    assert cfg.tanks[0].p10_stoiip == 14.0


@pytest.mark.parametrize(
    "tank",
    [
        {"name": "OS-top", "stoiip": 10.0, "p90_stoiip": 7.0},
        {"name": "OS-top", "stoiip": 10.0, "p10_stoiip": 14.0},
    ],
)
def test_volume_quantiles_must_be_given_together(tmp_path: Path, tank: dict) -> None:
    path = write_config(tmp_path, tanks=[tank])

    with pytest.raises(ValueError, match="must be given together"):
        ms.load_config(path)


@pytest.mark.parametrize(
    ("p90", "mean", "p10"),
    [
        (0.0, 10.0, 14.0),
        (10.0, 10.0, 14.0),
        (7.0, 10.0, 10.0),
    ],
)
def test_volume_quantiles_must_straddle_the_positive_mean(
    tmp_path: Path, p90: float, mean: float, p10: float
) -> None:
    path = write_config(
        tmp_path,
        tanks=[
            {
                "name": "OS-top",
                "stoiip": mean,
                "p90_stoiip": p90,
                "p10_stoiip": p10,
            }
        ],
    )

    with pytest.raises(ValueError, match="require 0 < p90_stoiip"):
        ms.load_config(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"n_realizations": 0}, "n_realizations must be greater than zero"),
        ({"sampling": "random"}, "sampling must be lhs or mc"),
    ],
)
def test_invalid_ensemble_controls_are_rejected(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    path = write_config(tmp_path, **overrides)

    with pytest.raises(ValueError, match=message):
        ms.load_config(path)


def test_duplicate_tank_names_are_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        tanks=[{"name": "OS-top", "stoiip": 1.0}, {"name": "OS-top", "stoiip": 2.0}],
    )
    with pytest.raises(ValueError, match="duplicate tank name"):
        ms.load_config(path)


def test_duplicate_control_names_are_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        controls=[
            {"name": "gas_lift", "tag": "MBAL.A", "value": 1},
            {"name": "gas_lift", "tag": "MBAL.B", "value": 2},
        ],
    )
    with pytest.raises(ValueError, match="duplicate control name"):
        ms.load_config(path)


def test_control_without_value_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, controls=[{"name": "gas_lift", "tag": "MBAL.X.GASLIFTRATE"}]
    )
    with pytest.raises(ValueError, match="value is required"):
        ms.load_config(path)


def test_empty_read_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, results={"stream": STREAM, "read": {}})
    with pytest.raises(ValueError, match="at least one variable"):
        ms.load_config(path)


# --- tags -----------------------------------------------------------------


def test_tank_input_tags_match_the_model(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            tanks=[
                {
                    "name": "OS-top",
                    "stoiip": 4.5,
                    "aquifer_volume": 120.0,
                    "rock_compressibility": 3.5e-6,
                }
            ],
        )
    )
    tags = [tag for _label, tag, _value in ms.tank_inputs(cfg)]
    assert tags == [
        "MBAL.MB[0].TANK[{OS-top}].OOIP",
        "MBAL.MB[0].TANK[{OS-top}].AQUIF.VOLUME",
        "MBAL.MB[0].TANK[{OS-top}].ROCKCOMPRESS",
    ]


def test_only_configured_tank_inputs_are_written(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))  # stoiip only
    assert [label for label, _tag, _value in ms.tank_inputs(cfg)] == [
        "OS-top.OOIP",
        "OS-bottom.OOIP",
    ]


def test_result_tags_use_the_stream_and_step(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.count_tag(cfg) == f"{STREAM}.COUNT"
    assert ms.result_tag(cfg, "CUMOIL", 24) == f"{STREAM}[24].CUMOIL"


def test_units_are_appended_when_given(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path, units={"stoiip": "MMstb"}))
    assert ms.tank_input_tag(cfg, cfg.tanks[0], "stoiip") == (
        'MBAL.MB[0].TANK[{OS-top}].OOIP("MMstb")'
    )


def test_no_units_means_no_qualifier(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.tank_input_tag(cfg, cfg.tanks[0], "stoiip") == (
        "MBAL.MB[0].TANK[{OS-top}].OOIP"
    )


# --- runs -----------------------------------------------------------------


def test_baseline_writes_nothing(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()
    ms.cmd_baseline(cfg, srv)
    assert srv.writes == []
    assert ms.RUN_TAG in srv.commands


def test_run_writes_every_input_and_control(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            tanks=[{"name": "OS-top", "stoiip": 4.5, "aquifer_volume": 120.0}],
            controls=[
                {
                    "name": "gas_lift",
                    "tag": "MBAL.MB[0].PREDWELL[{OP-OS1}].GASLIFTRATE",
                    "value": 0.5,
                },
                {
                    "name": "winj_max_rate",
                    "tag": "MBAL.MB[0].PREDWELL[{WI-OS1}].CONSTRAINTS.MAXRATE",
                    "value": 3000,
                },
            ],
        )
    )
    srv = FakeMBAL({"OS-top": 4.5})
    ms.cmd_run(cfg, srv)
    written = dict(srv.writes)
    assert written["MBAL.MB[0].TANK[{OS-top}].OOIP"] == 4.5
    assert written["MBAL.MB[0].TANK[{OS-top}].AQUIF.VOLUME"] == 120.0
    assert written["MBAL.MB[0].PREDWELL[{OP-OS1}].GASLIFTRATE"] == 0.5
    assert (
        written["MBAL.MB[0].PREDWELL[{WI-OS1}].CONSTRAINTS.MAXRATE"] == 3000
    )


def test_results_come_from_the_last_time_step(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    results = ms.cmd_baseline(cfg, FakeMBAL())
    # Fake MBAL: CUMOIL = total OOIP * 0.05 * (step + 1), last step is STEPS-1.
    assert results["cum_oil"] == pytest.approx((4.5 + 3.0) * 0.05 * STEPS)


def test_a_date_result_stays_a_string(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            results={"stream": STREAM, "read": {"date": "TIME", "cum_oil": "CUMOIL"}},
        )
    )
    results = ms.cmd_baseline(cfg, FakeMBAL())
    assert results["date"] == f"2026-0{STEPS}-01"


def test_match_passes_when_the_yaml_holds_the_official_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.cmd_match(cfg, FakeMBAL()) == 0
    assert "DIFF" not in capsys.readouterr().out


def test_match_fails_when_the_model_holds_different_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL({"OS-top": 6.0, "OS-bottom": 3.0})  # not the YAML numbers
    assert ms.cmd_match(cfg, srv) == 1
    assert "DIFF" in capsys.readouterr().out


def test_check_reports_the_model_inputs_against_the_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.cmd_check(cfg, FakeMBAL()) == 0
    srv = FakeMBAL({"OS-top": 6.0, "OS-bottom": 3.0})
    assert ms.cmd_check(cfg, srv) == 1
    assert srv.writes == []
    assert "6" in capsys.readouterr().out


def test_run_rejects_a_value_mbal_did_not_accept(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))

    class Stubborn(FakeMBAL):
        def set(self, tag: str, value: float | str) -> None:  # ignores writes
            self.writes.append((tag, value))

    # The model sits at other numbers, so an ignored write stays visible.
    with pytest.raises(RuntimeError, match="did not accept"):
        ms.cmd_run(cfg, Stubborn({"OS-top": 6.0, "OS-bottom": 3.0}))


def test_missing_prediction_results_are_an_error(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))

    class Empty(FakeMBAL):
        def get_raw(self, tag: str) -> float | str:
            if tag.endswith(".COUNT"):
                return 0.0
            return super().get_raw(tag)

    srv = Empty()
    srv.cmd(ms.RUN_TAG)
    with pytest.raises(RuntimeError, match="COUNT"):
        ms.read_results(cfg, srv)


def test_profile_writes_one_row_per_time_step(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            results={
                "stream": STREAM,
                "read": {"cum_oil": "CUMOIL"},
                "profile": True,
            },
        )
    )
    ms.cmd_baseline(cfg, FakeMBAL())
    rows = (Path(cfg.out_dir) / "profile_baseline.csv").read_text().splitlines()
    assert rows[0] == "step,cum_oil"
    assert len(rows) == STEPS + 1


def test_each_run_appends_one_row(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()
    ms.cmd_baseline(cfg, srv)
    ms.cmd_run(cfg, srv)
    rows = (Path(cfg.out_dir) / "simple_results.csv").read_text().strip().splitlines()
    assert len(rows) == 3  # header + baseline + run
    assert "baseline" in rows[1]
    assert "run" in rows[2]
