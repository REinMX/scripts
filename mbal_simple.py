"""
Simple MBAL runner
==================

Run an existing MBAL prediction from a small YAML file. No sampling, no
statistics: one set of numbers in, one prediction, one row of results out.

The first job is trust: prove that a prediction driven from Python gives the
same answer as the same prediction run by hand in MBAL.

    python mbal_simple.py simple.yaml --show       # resolved tags, no MBAL
    python mbal_simple.py simple.yaml --check      # read model, compare inputs
    python mbal_simple.py simple.yaml --baseline   # official run, writes nothing
    python mbal_simple.py simple.yaml --run        # write YAML values, predict
    python mbal_simple.py simple.yaml --match      # baseline vs run, side by side

MBAL must already be open. OpenServer attaches to a running MBAL and cannot
start one.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# Per-tank inputs, as YAML key -> the suffix appended to MBAL.MB[0].TANK[ref].
TANK_INPUTS = {
    "stoiip": "OOIP",
    "aquifer_volume": "AQUIF.VOLUME",
    "rock_compressibility": "ROCKCOMPRESS",
}
TANK_KEYS = {"name", "index", "p90_stoiip", "p10_stoiip", *TANK_INPUTS}
CONTROL_KEYS = {"name", "tag", "value"}
RESULTS_KEYS = {"stream", "read", "profile"}
CONFIG_KEYS = {
    "mbal_file",
    "tanks",
    "controls",
    "results",
    "units",
    "tag_mode",
    "out_dir",
    "tolerance_pct",
    "prog_id",
    "n_realizations",
    "seed",
    "sampling",
}

# One field-level prediction stream: TRES[stream][sheet], both named in this
# model. The trailing [k] on a variable is the row, i.e. the time step.
DEFAULT_STREAM = "MBAL.MB[0].TRES[{Prediction}][{Prediction}]"
DEFAULT_READ = {"cum_oil": "CUMOIL", "res_pres": "RESPRESS"}


@dataclass(frozen=True)
class Tank:
    name: str
    index: int | None = None
    # Only the inputs actually given in the YAML are written.
    inputs: dict[str, float] = field(default_factory=dict)
    # Optional O&G low/high anchors for ensemble sampling. ``stoiip`` remains
    # the deterministic value used by this simple runner and is interpreted as
    # the volume mean by mbal_ensemble.py.
    p90_stoiip: float | None = None
    p10_stoiip: float | None = None


@dataclass(frozen=True)
class Control:
    name: str
    tag: str
    value: float | str


@dataclass(frozen=True)
class Results:
    """The prediction stream and the variables read from its last step."""

    stream: str = DEFAULT_STREAM
    read: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_READ))
    profile: bool = False


@dataclass(frozen=True)
class Config:
    mbal_file: str
    tanks: tuple[Tank, ...]
    controls: tuple[Control, ...] = ()
    results: Results = field(default_factory=Results)
    units: dict[str, str] = field(default_factory=dict)
    tag_mode: str = "name"
    out_dir: str = "simple_output"
    tolerance_pct: float = 0.1
    prog_id: str = "PX32.OpenServer.1"
    n_realizations: int = 200
    seed: int = 42
    sampling: str = "lhs"


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def _parse_tank(item: Any, where: str) -> Tank:
    if not isinstance(item, dict):
        raise TypeError(f"{where}: must be a mapping")
    _reject_unknown(item, TANK_KEYS, where)
    if not item.get("name"):
        raise ValueError(f"{where}: name is required")
    if "stoiip" not in item:
        raise ValueError(f"{where}: stoiip is required")

    inputs: dict[str, float] = {}
    for key in TANK_INPUTS:
        if item.get(key) is None:
            continue
        value = float(item[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{where}: {key} must be a positive number")
        inputs[key] = value
    p90 = None if item.get("p90_stoiip") is None else float(item["p90_stoiip"])
    p10 = None if item.get("p10_stoiip") is None else float(item["p10_stoiip"])
    if (p90 is None) != (p10 is None):
        raise ValueError(
            f"{where}: p90_stoiip and p10_stoiip must be given together"
        )
    if p90 is not None and p10 is not None:
        mean = inputs["stoiip"]
        if not (
            math.isfinite(p90)
            and math.isfinite(p10)
            and 0.0 < p90 < mean < p10
        ):
            raise ValueError(
                f"{where}: require 0 < p90_stoiip < stoiip mean < p10_stoiip"
            )
    return Tank(
        name=str(item["name"]),
        index=None if item.get("index") is None else int(item["index"]),
        inputs=inputs,
        p90_stoiip=p90,
        p10_stoiip=p10,
    )


def _parse_control(item: Any, where: str) -> Control:
    if not isinstance(item, dict):
        raise TypeError(f"{where}: must be a mapping")
    _reject_unknown(item, CONTROL_KEYS, where)
    for required in ("name", "tag"):
        if not item.get(required):
            raise ValueError(f"{where}: {required} is required")
    if "value" not in item:
        raise ValueError(f"{where}: value is required")
    return Control(name=str(item["name"]), tag=str(item["tag"]), value=item["value"])


def _parse_results(raw: Any, where: str) -> Results:
    if raw is None:
        return Results()
    if not isinstance(raw, dict):
        raise TypeError(f"{where}: must be a mapping")
    _reject_unknown(raw, RESULTS_KEYS, where)
    # An absent read: falls back to the default; an empty one is a mistake.
    read = DEFAULT_READ if raw.get("read") is None else raw["read"]
    if not isinstance(read, dict):
        raise TypeError(f"{where}: read must be a mapping of name -> variable")
    if not read:
        raise ValueError(f"{where}: read must name at least one variable")
    return Results(
        stream=str(raw.get("stream", DEFAULT_STREAM)),
        read={str(name): str(variable) for name, variable in read.items()},
        profile=bool(raw.get("profile", False)),
    )


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: top level must be a mapping")
    _reject_unknown(raw, CONFIG_KEYS, str(path))

    if not raw.get("mbal_file"):
        raise ValueError(f"{path}: mbal_file is required")

    tanks = tuple(
        _parse_tank(item, f"{path}: tanks[{position}]")
        for position, item in enumerate(raw.get("tanks") or [])
    )
    if not tanks:
        raise ValueError(f"{path}: at least one tank is required")
    names = [tank.name for tank in tanks]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate tank name(s) {', '.join(duplicates)}")

    controls = tuple(
        _parse_control(item, f"{path}: controls[{position}]")
        for position, item in enumerate(raw.get("controls") or [])
    )
    control_names = [control.name for control in controls]
    repeated = sorted({name for name in control_names if control_names.count(name) > 1})
    if repeated:
        raise ValueError(f"{path}: duplicate control name(s) {', '.join(repeated)}")

    tag_mode = str(raw.get("tag_mode", "name")).lower()
    if tag_mode not in {"name", "index"}:
        raise ValueError(f"{path}: tag_mode must be name or index")
    if tag_mode == "index" and any(tank.index is None for tank in tanks):
        raise ValueError(f"{path}: tag_mode: index requires index on every tank")

    n_realizations = int(raw.get("n_realizations", 200))
    if n_realizations <= 0:
        raise ValueError(f"{path}: n_realizations must be greater than zero")
    sampling = str(raw.get("sampling", "lhs")).lower()
    if sampling not in {"lhs", "mc"}:
        raise ValueError(f"{path}: sampling must be lhs or mc")

    return Config(
        mbal_file=str(raw["mbal_file"]),
        tanks=tanks,
        controls=controls,
        results=_parse_results(raw.get("results"), f"{path}: results"),
        units=dict(raw.get("units") or {}),
        tag_mode=tag_mode,
        out_dir=str(raw.get("out_dir", "simple_output")),
        tolerance_pct=float(raw.get("tolerance_pct", 0.1)),
        prog_id=str(raw.get("prog_id", "PX32.OpenServer.1")),
        n_realizations=n_realizations,
        seed=int(raw.get("seed", 42)),
        sampling=sampling,
    )


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def _tank_ref(cfg: Config, tank: Tank) -> str:
    if cfg.tag_mode == "index":
        return str(tank.index)
    return "{" + tank.name + "}"


def tank_input_tag(cfg: Config, tank: Tank, key: str) -> str:
    """MBAL.MB[0].TANK[{OS-top}].OOIP, plus a unit qualifier when configured."""
    unit = cfg.units.get(key)
    qualifier = f'("{unit}")' if unit else ""
    return f"MBAL.MB[0].TANK[{_tank_ref(cfg, tank)}].{TANK_INPUTS[key]}{qualifier}"


def tank_inputs(cfg: Config) -> list[tuple[str, str, float]]:
    """Every configured tank input as (label, tag, value)."""
    return [
        (f"{tank.name}.{TANK_INPUTS[key]}", tank_input_tag(cfg, tank, key), value)
        for tank in cfg.tanks
        for key, value in tank.inputs.items()
    ]


def count_tag(cfg: Config) -> str:
    """Number of rows in the prediction stream: the time-step count."""
    return f"{cfg.results.stream}.COUNT"


def result_tag(cfg: Config, variable: str, step: int | str) -> str:
    """One variable at one time step: ...TRES[..][..][k].CUMOIL

    ``step`` is the row index; pass "k" to render the shape of the tag.
    """
    return f"{cfg.results.stream}[{step}].{variable}"


OPEN_TAG = 'MBAL.OPENFILE("{path}")'
RUN_TAG = "MBAL.MB.RunPrediction"


# --------------------------------------------------------------------------
# OpenServer
# --------------------------------------------------------------------------


class OpenServer:
    """Thin wrapper around the Petroleum Experts OpenServer COM object."""

    def __init__(self, prog_id: str = "PX32.OpenServer.1"):
        import win32com.client  # lazy: --show works without MBAL

        self.os = win32com.client.Dispatch(prog_id)

    def _check(self, code: int, what: str) -> None:
        if code:
            description = "(no description available)"
            with suppress(Exception):
                description = self.os.GetErrorDescription(code)
            raise RuntimeError(f"OpenServer error {code} on {what}: {description}")

    def cmd(self, command: str) -> None:
        self._check(self.os.DoCommand(command), command)

    def set(self, tag: str, value: float | str) -> None:
        self._check(self.os.SetValue(tag, value), f"SetValue {tag}")

    def get_raw(self, tag: str) -> Any:
        value = self.os.GetValue(tag)
        error = self.os.GetLastError("MBAL")
        if error:
            raise RuntimeError(
                f"OpenServer error reading {tag}: "
                f"{self.os.GetErrorDescription(error)}"
            )
        return value

    def get(self, tag: str) -> float:
        return float(self.get_raw(tag))


def open_model(cfg: Config, srv: Any) -> None:
    """Reload the model from disk, discarding anything a previous run wrote."""
    path = cfg.mbal_file.replace('"', '\\"')
    try:
        srv.cmd(OPEN_TAG.format(path=path))
    except RuntimeError as error:
        raise RuntimeError(
            f"could not open {cfg.mbal_file}: {error}. OpenServer attaches to a "
            "running MBAL, it does not start one. Launch MBAL, clear any "
            "startup dialog, leave it open, and run this again."
        ) from error


def _as_number(value: Any) -> float | str:
    """Numbers come back as floats; dates and keywords stay strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------
# Read / write / run
# --------------------------------------------------------------------------


def read_inputs(cfg: Config, srv: Any) -> dict[str, float]:
    """The tank inputs MBAL currently holds, for every one this YAML sets."""
    return {label: srv.get(tag) for label, tag, _value in tank_inputs(cfg)}


def write_inputs(cfg: Config, srv: Any) -> list[str]:
    """Write every tank input and control, then read each one back.

    Returns a description of every value MBAL did not take.
    """
    mismatched: list[str] = []
    for _label, tag, value in tank_inputs(cfg):
        srv.set(tag, value)
        read_back = srv.get(tag)
        if not _close(read_back, value, cfg.tolerance_pct):
            mismatched.append(f"{tag} -> wrote {value}, read {read_back}")
    for control in cfg.controls:
        srv.set(control.tag, control.value)
        if isinstance(control.value, bool) or not isinstance(
            control.value, (int, float)
        ):
            continue  # MBAL may normalise a keyword; only numbers are comparable
        read_back = srv.get(control.tag)
        if not _close(read_back, float(control.value), cfg.tolerance_pct):
            mismatched.append(
                f"{control.tag} -> wrote {control.value}, read {read_back}"
            )
    return mismatched


def step_count(cfg: Config, srv: Any) -> int:
    tag = count_tag(cfg)
    steps = int(srv.get(tag))
    if steps <= 0:
        raise RuntimeError(
            f"prediction step COUNT from {tag} must be positive, got {steps}. "
            "The prediction produced no rows, or the stream name is wrong."
        )
    return steps


def read_results(cfg: Config, srv: Any) -> dict[str, float | str]:
    """Every configured variable at the last time step of the prediction."""
    last = step_count(cfg, srv) - 1
    return {
        name: _as_number(srv.get_raw(result_tag(cfg, variable, last)))
        for name, variable in cfg.results.read.items()
    }


def read_profile(cfg: Config, srv: Any) -> list[dict[str, Any]]:
    """Every configured variable at every time step."""
    steps = step_count(cfg, srv)
    rows: list[dict[str, Any]] = []
    for step in range(steps):
        row: dict[str, Any] = {"step": step}
        for name, variable in cfg.results.read.items():
            row[name] = _as_number(srv.get_raw(result_tag(cfg, variable, step)))
        rows.append(row)
    return rows


def run_prediction(cfg: Config, srv: Any) -> dict[str, float | str]:
    srv.cmd(RUN_TAG)
    return read_results(cfg, srv)


def _close(a: float | str, b: float | str, tolerance_pct: float) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    if b == 0:
        return abs(a) <= 1e-12
    return abs(a - b) / abs(b) * 100.0 <= tolerance_pct


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt(value: float | str) -> str:
    if isinstance(value, str):
        return value
    if not math.isfinite(value):
        return "n/a"
    return f"{value:,.4g}"


def compare_table(
    title: str,
    left_label: str,
    right_label: str,
    left: dict[str, float | str],
    right: dict[str, float | str],
    tolerance_pct: float,
) -> tuple[str, bool]:
    """Side-by-side table plus a flag: True when every row is within tolerance."""
    keys = list(left)
    width = max((len(key) for key in keys), default=4)
    header = (
        f"{'item'.ljust(width)}  {left_label:>14}  {right_label:>14}  "
        f"{'diff':>12}  {'diff %':>8}  status"
    )
    lines = [title, "-" * len(header), header, "-" * len(header)]
    all_ok = True
    for key in keys:
        a = left[key]
        b = right.get(key, float("nan"))
        numeric = not (isinstance(a, str) or isinstance(b, str))
        diff = b - a if numeric else float("nan")
        pct = (
            diff / a * 100.0
            if numeric and math.isfinite(a) and a != 0
            else float("nan")
        )
        ok = _close(b, a, tolerance_pct)
        all_ok = all_ok and ok
        lines.append(
            f"{key.ljust(width)}  {_fmt(a):>14}  {_fmt(b):>14}  "
            f"{_fmt(diff):>12}  {_fmt(pct):>8}  {'MATCH' if ok else 'DIFF'}"
        )
    lines.append("-" * len(header))
    return "\n".join(lines), all_ok


def write_row(cfg: Config, mode: str, row: dict[str, Any]) -> Path:
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "simple_results.csv"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        **row,
    }
    existing = []
    fieldnames = list(record)
    if path.exists():
        with path.open(newline="") as handle:
            existing = list(csv.DictReader(handle))
        for name in existing[0] if existing else []:
            if name not in fieldnames:
                fieldnames.append(name)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerow(record)
    return path


def write_profile(cfg: Config, mode: str, rows: list[dict[str, Any]]) -> Path:
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"profile_{mode}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_show(cfg: Config) -> int:
    print(f"model      {cfg.mbal_file}")
    print(f"tag_mode   {cfg.tag_mode}")
    print(f"tolerance  {cfg.tolerance_pct}%")
    print(f"units      {cfg.units or '(model defaults - set units: to be sure)'}")

    print("\ntank inputs written by --run:")
    for _label, tag, value in tank_inputs(cfg):
        print(f"  {value:>14}  {tag}")

    print("\ncontrols written by --run:")
    for control in cfg.controls:
        print(f"  {control.value!s:>14}  {control.tag}   ({control.name})")
    if not cfg.controls:
        print("  (none)")

    print("\nresults read at the last time step:")
    print(f"  {'steps':>14}  {count_tag(cfg)}")
    for name, variable in cfg.results.read.items():
        print(f"  {name:>14}  {result_tag(cfg, variable, 'k')}")
    if cfg.results.profile:
        print("  (profile: true - every step is also written to profile_*.csv)")

    if not cfg.units:
        print(
            "\nNo units: set. MBAL then answers in the model's current unit "
            "set, which is the most common reason Python and the GUI disagree."
        )
    return 0


def cmd_check(cfg: Config, srv: Any) -> int:
    open_model(cfg, srv)
    in_model = read_inputs(cfg, srv)
    in_yaml = {label: value for label, _tag, value in tank_inputs(cfg)}
    table, ok = compare_table(
        "Tank inputs: what MBAL holds vs what this YAML would write",
        "in MBAL",
        "in YAML",
        in_model,
        in_yaml,
        cfg.tolerance_pct,
    )
    print(table)
    if ok:
        print("Inputs match. --run will not change these values in the model.")
        return 0
    print(
        "Inputs differ. Either the YAML numbers are not the official ones, or "
        "the units differ (set units: stoiip: ...). Fix this before comparing "
        "prediction results."
    )
    return 1


def _finish(
    cfg: Config,
    srv: Any,
    mode: str,
    inputs: dict[str, Any],
    elapsed: float,
    results: dict[str, float | str],
) -> None:
    write_row(cfg, mode, {**inputs, **results, "runtime_s": round(elapsed, 2)})
    if cfg.results.profile:
        path = write_profile(cfg, mode, read_profile(cfg, srv))
        print(f"profile written to {path}")


def cmd_baseline(cfg: Config, srv: Any) -> dict[str, float | str]:
    """The official run: reload the model, predict, write nothing."""
    open_model(cfg, srv)
    in_model = read_inputs(cfg, srv)
    start = time.time()
    results = run_prediction(cfg, srv)
    elapsed = time.time() - start
    print(f"baseline prediction ran in {elapsed:.1f}s (no inputs written)")
    _finish(cfg, srv, "baseline", in_model, elapsed, results)
    return results


def cmd_run(cfg: Config, srv: Any) -> dict[str, float | str]:
    """Write the YAML inputs and controls, then predict."""
    open_model(cfg, srv)
    mismatched = write_inputs(cfg, srv)
    if mismatched:
        raise RuntimeError(
            "MBAL did not accept these written values:\n  "
            + "\n  ".join(mismatched)
            + "\nUsually a unit qualifier or a wrong access string."
        )
    start = time.time()
    results = run_prediction(cfg, srv)
    elapsed = time.time() - start
    print(f"yaml prediction ran in {elapsed:.1f}s ({len(cfg.controls)} controls)")
    inputs: dict[str, Any] = {
        label: value for label, _tag, value in tank_inputs(cfg)
    }
    inputs.update({control.name: control.value for control in cfg.controls})
    _finish(cfg, srv, "run", inputs, elapsed, results)
    return results


def cmd_match(cfg: Config, srv: Any) -> int:
    print("1/2 official run, as the model is saved")
    baseline = cmd_baseline(cfg, srv)
    print("\n2/2 same prediction, inputs written from YAML")
    run = cmd_run(cfg, srv)
    print()
    table, ok = compare_table(
        "Prediction results: official run vs YAML run",
        "official",
        "from YAML",
        baseline,
        run,
        cfg.tolerance_pct,
    )
    print(table)
    if ok:
        print(
            f"\nMatched within {cfg.tolerance_pct}%. The YAML reproduces the "
            "official run, so anything it changes from here is a real effect."
        )
        return 0
    print(
        f"\nDid not match within {cfg.tolerance_pct}%. Run --check first: if the "
        "tank inputs already differ, the YAML is not at the official numbers. "
        "If they match, a control in the YAML is changing the prediction."
    )
    return 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", help="YAML config file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--show",
        action="store_true",
        help="print the resolved tags and values; never opens MBAL",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="read the model's tank inputs and compare them with the YAML",
    )
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="run the prediction exactly as saved; writes nothing",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="write the YAML inputs and controls, then run the prediction",
    )
    mode.add_argument(
        "--match",
        action="store_true",
        help="baseline then run, reported side by side",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.show or not any((args.check, args.baseline, args.run, args.match)):
        return cmd_show(cfg)

    srv = OpenServer(cfg.prog_id)
    if args.check:
        return cmd_check(cfg, srv)
    if args.match:
        return cmd_match(cfg, srv)

    results = cmd_baseline(cfg, srv) if args.baseline else cmd_run(cfg, srv)
    width = max(len(key) for key in results)
    print()
    for key, value in results.items():
        print(f"  {key.ljust(width)}  {_fmt(value)}")
    print(f"\nwritten to {Path(cfg.out_dir) / 'simple_results.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
