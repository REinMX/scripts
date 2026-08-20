"""Tests for the simple deterministic runner.

A fake OpenServer stands in for MBAL: it stores whatever is written and, on
RunPrediction, produces results derived from the STOIIP it currently holds.
That is enough to prove the baseline writes nothing, that --run writes the
YAML values, and that --match sees a difference when there is one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import mbal_simple as ms

TANKS = {"TankA": 4.5, "TankB": 3.0}
STEPS = 3


class FakeMBAL:
    """Minimal MBAL: OOIP in, cumulative oil and pressure out."""

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
        if not key.startswith("ooip::"):
            self.store[key] = value
            return
        self.store[key] = float(value)

    def get(self, tag: str) -> float:
        key = self._key(tag)
        if key.startswith("ooip::"):
            return float(self.store[key])
        if key.startswith("count::"):
            self._require_prediction(tag)
            return float(STEPS)
        match = re.fullmatch(r"(np|pres)::(.+)::(\d+)", key)
        if match:
            self._require_prediction(tag)
            kind, name, step = match.group(1), match.group(2), int(match.group(3))
            ooip = float(self.store[f"ooip::{name}"])
            # Recovery grows with step; pressure falls with cumulative offtake.
            if kind == "np":
                return ooip * 0.05 * (step + 1)
            return 3000.0 - 100.0 * (step + 1) * (4.5 / ooip)
        if key in self.store:
            return float(self.store[key])
        raise RuntimeError(f"unreadable tag {tag}")

    def _require_prediction(self, tag: str) -> None:
        if not self.predicted:
            raise RuntimeError(f"no prediction results for {tag}")

    @staticmethod
    def _key(tag: str) -> str:
        """Collapse a real access string into a simple store key."""
        ooip = re.fullmatch(r"MBAL\.MB\[0\]\.TANK\[\{(.+?)\}\]\.OOIP(\(.*\))?", tag)
        if ooip:
            return f"ooip::{ooip.group(1)}"
        count = re.fullmatch(r"MBAL\.MB\[0\]\.TRES\[2\]\[\{(.+?)\}\]\.COUNT", tag)
        if count:
            return f"count::{count.group(1)}"
        result = re.fullmatch(
            r"MBAL\.MB\[0\]\.TRES\[2\]\[\{(.+?)\}\]\[(\d+)\]\."
            r"(OILRECOVER|TANKPRESS)(\(.*\))?",
            tag,
        )
        if result:
            kind = "np" if result.group(3) == "OILRECOVER" else "pres"
            return f"{kind}::{result.group(1)}::{result.group(2)}"
        return tag


def write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "mbal_file": r"C:\Work\model.mbi",
        "units": {"stoiip": "MMstb", "oil": "MMstb", "pressure": "psia"},
        "tanks": [{"name": name, "stoiip": value} for name, value in TANKS.items()],
        "out_dir": str(tmp_path / "out"),
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# --- config ---------------------------------------------------------------


def test_unknown_tank_key_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, tanks=[{"name": "TankA", "stoip": 4.5}])
    with pytest.raises(ValueError, match="unknown key"):
        ms.load_config(path)


def test_missing_stoiip_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, tanks=[{"name": "TankA"}])
    with pytest.raises(ValueError, match="stoiip is required"):
        ms.load_config(path)


def test_duplicate_tank_names_are_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        tanks=[{"name": "TankA", "stoiip": 1.0}, {"name": "TankA", "stoiip": 2.0}],
    )
    with pytest.raises(ValueError, match="duplicate tank name"):
        ms.load_config(path)


def test_control_without_value_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, controls=[{"name": "gas_lift", "tag": "MBAL.X.MAX_GASLIFT"}]
    )
    with pytest.raises(ValueError, match="value is required"):
        ms.load_config(path)


# --- tags -----------------------------------------------------------------


def test_units_appear_in_every_tag(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    tank = cfg.tanks[0]
    assert ms.stoiip_tag(cfg, tank) == 'MBAL.MB[0].TANK[{TankA}].OOIP("MMstb")'
    assert ms.oil_tag(cfg, tank, 2) == (
        'MBAL.MB[0].TRES[2][{TankA}][2].OILRECOVER("MMstb")'
    )
    assert ms.pressure_tag(cfg, tank, 2) == (
        'MBAL.MB[0].TRES[2][{TankA}][2].TANKPRESS("psia")'
    )
    assert ms.count_tag(cfg, tank) == "MBAL.MB[0].TRES[2][{TankA}].COUNT"


def test_no_units_means_no_qualifier(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path, units={}))
    assert ms.stoiip_tag(cfg, cfg.tanks[0]) == "MBAL.MB[0].TANK[{TankA}].OOIP"


def test_result_sheet_index_overrides_the_name(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            tanks=[{"name": "TankA", "stoiip": 4.5, "result_sheet": 1}],
        )
    )
    assert ms.count_tag(cfg, cfg.tanks[0]) == "MBAL.MB[0].TRES[2][1].COUNT"


# --- runs -----------------------------------------------------------------


def test_baseline_writes_nothing(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()
    ms.cmd_baseline(cfg, srv)
    assert srv.writes == []
    assert ms.RUN_TAG in srv.commands


def test_run_writes_every_volume_and_control(tmp_path: Path) -> None:
    cfg = ms.load_config(
        write_config(
            tmp_path,
            controls=[
                {"name": "gas_lift", "tag": "MBAL.X.MAX_GASLIFT", "value": 0.5},
                {"name": "pred_watinj", "tag": "MBAL.X.WATINJ", "value": "YES"},
            ],
        )
    )
    srv = FakeMBAL()
    ms.cmd_run(cfg, srv)
    written = dict(srv.writes)
    assert written['MBAL.MB[0].TANK[{TankA}].OOIP("MMstb")'] == 4.5
    assert written['MBAL.MB[0].TANK[{TankB}].OOIP("MMstb")'] == 3.0
    assert written["MBAL.X.MAX_GASLIFT"] == 0.5
    assert written["MBAL.X.WATINJ"] == "YES"


def test_results_use_the_last_prediction_step(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()
    results = ms.cmd_baseline(cfg, srv)
    # Fake MBAL: Np = OOIP * 0.05 * (step + 1), last step is STEPS - 1.
    assert results["np_TankA"] == pytest.approx(4.5 * 0.05 * STEPS)
    assert results["np_total"] == pytest.approx((4.5 + 3.0) * 0.05 * STEPS)


def test_match_passes_when_the_yaml_holds_the_official_volumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.cmd_match(cfg, FakeMBAL()) == 0
    out = capsys.readouterr().out
    assert "DIFF" not in out


def test_match_fails_when_the_model_holds_different_volumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL({"TankA": 6.0, "TankB": 3.0})  # model is not at the YAML numbers
    assert ms.cmd_match(cfg, srv) == 1
    assert "DIFF" in capsys.readouterr().out


def test_check_reports_the_model_volumes_against_the_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    assert ms.cmd_check(cfg, FakeMBAL()) == 0
    srv = FakeMBAL({"TankA": 6.0, "TankB": 3.0})
    assert ms.cmd_check(cfg, srv) == 1
    assert srv.writes == []
    assert "6" in capsys.readouterr().out


def test_run_rejects_a_value_mbal_did_not_accept(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))

    class Stubborn(FakeMBAL):
        def set(self, tag: str, value: float | str) -> None:  # ignores writes
            self.writes.append((tag, value))

    # The model sits at other volumes, so an ignored write stays visible.
    with pytest.raises(RuntimeError, match="did not accept"):
        ms.cmd_run(cfg, Stubborn({"TankA": 6.0, "TankB": 3.0}))


def test_missing_prediction_results_are_an_error(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()

    class Empty(FakeMBAL):
        def get(self, tag: str) -> float:
            if ".COUNT" in tag:
                return 0.0
            return super().get(tag)

    srv = Empty()
    srv.cmd(ms.RUN_TAG)
    with pytest.raises(RuntimeError, match="COUNT"):
        ms.read_results(cfg, srv)


def test_each_run_appends_one_row(tmp_path: Path) -> None:
    cfg = ms.load_config(write_config(tmp_path))
    srv = FakeMBAL()
    ms.cmd_baseline(cfg, srv)
    ms.cmd_run(cfg, srv)
    path = Path(cfg.out_dir) / "simple_results.csv"
    rows = path.read_text().strip().splitlines()
    assert len(rows) == 3  # header + baseline + run
    assert "baseline" in rows[1]
    assert "run" in rows[2]
