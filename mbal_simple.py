"""
Simple MBAL runner
==================

Run an existing MBAL prediction from a small YAML file. No sampling, no
statistics: one set of numbers in, one prediction, one row of results out.

The first job is trust: prove that a prediction driven from Python gives the
same answer as the same prediction run by hand in MBAL.

    python mbal_simple.py simple.yaml --show       # resolved tags, no MBAL
    python mbal_simple.py simple.yaml --check      # read model, compare volumes
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

TANK_KEYS = {"name", "stoiip", "index", "result_sheet"}
CONTROL_KEYS = {"name", "tag", "value"}
RESULT_KEYS = {"name", "tag", "tank"}
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
}


@dataclass(frozen=True)
class Tank:
    name: str
    stoiip: float
    index: int | None = None
    result_sheet: int | str | None = None


@dataclass(frozen=True)
class Control:
    name: str
    tag: str
    value: float | str


@dataclass(frozen=True)
class ExtraResult:
    name: str
    tag: str
    tank: str | None = None


@dataclass(frozen=True)
class Config:
    mbal_file: str
    tanks: tuple[Tank, ...]
    controls: tuple[Control, ...] = ()
    results: tuple[ExtraResult, ...] = ()
    units: dict[str, str] = field(default_factory=dict)
    tag_mode: str = "name"
    out_dir: str = "simple_output"
    tolerance_pct: float = 0.1
    prog_id: str = "PX32.OpenServer.1"


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: top level must be a mapping")
    _reject_unknown(raw, CONFIG_KEYS, str(path))

    if not raw.get("mbal_file"):
        raise ValueError(f"{path}: mbal_file is required")

    tanks: list[Tank] = []
    for position, item in enumerate(raw.get("tanks") or []):
        where = f"{path}: tanks[{position}]"
        if not isinstance(item, dict):
            raise TypeError(f"{where}: must be a mapping")
        _reject_unknown(item, TANK_KEYS, where)
        if not item.get("name"):
            raise ValueError(f"{where}: name is required")
        if "stoiip" not in item:
            raise ValueError(f"{where}: stoiip is required")
        stoiip = float(item["stoiip"])
        if not math.isfinite(stoiip) or stoiip <= 0:
            raise ValueError(f"{where}: stoiip must be a positive number")
        tanks.append(
            Tank(
                name=str(item["name"]),
                stoiip=stoiip,
                index=None if item.get("index") is None else int(item["index"]),
                result_sheet=item.get("result_sheet"),
            )
        )
    if not tanks:
        raise ValueError(f"{path}: at least one tank is required")

    names = [tank.name for tank in tanks]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate tank name(s) {', '.join(duplicates)}")

    controls: list[Control] = []
    for position, item in enumerate(raw.get("controls") or []):
        where = f"{path}: controls[{position}]"
        if not isinstance(item, dict):
            raise TypeError(f"{where}: must be a mapping")
        _reject_unknown(item, CONTROL_KEYS, where)
        for required in ("name", "tag"):
            if not item.get(required):
                raise ValueError(f"{where}: {required} is required")
        if "value" not in item:
            raise ValueError(f"{where}: value is required")
        controls.append(
            Control(
                name=str(item["name"]),
                tag=str(item["tag"]),
                value=item["value"],
            )
        )

    results: list[ExtraResult] = []
    for position, item in enumerate(raw.get("results") or []):
        where = f"{path}: results[{position}]"
        if not isinstance(item, dict):
            raise TypeError(f"{where}: must be a mapping")
        _reject_unknown(item, RESULT_KEYS, where)
        for required in ("name", "tag"):
            if not item.get(required):
                raise ValueError(f"{where}: {required} is required")
        tank_name = item.get("tank")
        if tank_name is not None and str(tank_name) not in names:
            raise ValueError(f"{where}: tank {tank_name} is not in tanks:")
        results.append(
            ExtraResult(
                name=str(item["name"]),
                tag=str(item["tag"]),
                tank=None if tank_name is None else str(tank_name),
            )
        )

    tag_mode = str(raw.get("tag_mode", "name")).lower()
    if tag_mode not in {"name", "index"}:
        raise ValueError(f"{path}: tag_mode must be name or index")
    if tag_mode == "index" and any(tank.index is None for tank in tanks):
        raise ValueError(f"{path}: tag_mode: index requires index on every tank")

    return Config(
        mbal_file=str(raw["mbal_file"]),
        tanks=tuple(tanks),
        controls=tuple(controls),
        results=tuple(results),
        units=dict(raw.get("units") or {}),
        tag_mode=tag_mode,
        out_dir=str(raw.get("out_dir", "simple_output")),
        tolerance_pct=float(raw.get("tolerance_pct", 0.1)),
        prog_id=str(raw.get("prog_id", "PX32.OpenServer.1")),
    )


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def _unit(cfg: Config, key: str) -> str:
    unit = cfg.units.get(key)
    return f'("{unit}")' if unit else ""


def _tank_ref(cfg: Config, tank: Tank) -> str:
    if cfg.tag_mode == "index":
        return str(tank.index)
    return "{" + tank.name + "}"


def _sheet_ref(cfg: Config, tank: Tank) -> str:
    if tank.result_sheet is None:
        return _tank_ref(cfg, tank)
    if isinstance(tank.result_sheet, int):
        return str(tank.result_sheet)
    return "{" + str(tank.result_sheet) + "}"


def stoiip_tag(cfg: Config, tank: Tank) -> str:
    return f"MBAL.MB[0].TANK[{_tank_ref(cfg, tank)}].OOIP{_unit(cfg, 'stoiip')}"


def count_tag(cfg: Config, tank: Tank) -> str:
    return f"MBAL.MB[0].TRES[2][{_sheet_ref(cfg, tank)}].COUNT"


def oil_tag(cfg: Config, tank: Tank, step: int) -> str:
    ref = _sheet_ref(cfg, tank)
    return f"MBAL.MB[0].TRES[2][{ref}][{step}].OILRECOVER{_unit(cfg, 'oil')}"


def pressure_tag(cfg: Config, tank: Tank, step: int) -> str:
    ref = _sheet_ref(cfg, tank)
    return f"MBAL.MB[0].TRES[2][{ref}][{step}].TANKPRESS{_unit(cfg, 'pressure')}"


def extra_tag(cfg: Config, result: ExtraResult, last_step: dict[str, int]) -> str:
    tag = result.tag
    if result.tank is not None:
        tank = next(item for item in cfg.tanks if item.name == result.tank)
        tag = tag.replace("{tank}", _tank_ref(cfg, tank))
        tag = tag.replace("{sheet}", _sheet_ref(cfg, tank))
        tag = tag.replace("{k}", str(last_step[result.tank]))
    return tag


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

    def get(self, tag: str) -> float:
        value = self.os.GetValue(tag)
        error = self.os.GetLastError("MBAL")
        if error:
            raise RuntimeError(
                f"OpenServer error reading {tag}: "
                f"{self.os.GetErrorDescription(error)}"
            )
        return float(value)


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


# --------------------------------------------------------------------------
# Read / write / run
# --------------------------------------------------------------------------


def read_volumes(cfg: Config, srv: Any) -> dict[str, float]:
    """The STOIIP MBAL currently holds, tank by tank."""
    return {tank.name: srv.get(stoiip_tag(cfg, tank)) for tank in cfg.tanks}


def write_inputs(cfg: Config, srv: Any) -> list[str]:
    """Write every YAML volume and control, then read each one back.

    Returns the tags whose read-back did not match what was written.
    """
    mismatched: list[str] = []
    for tank in cfg.tanks:
        tag = stoiip_tag(cfg, tank)
        srv.set(tag, tank.stoiip)
        read_back = srv.get(tag)
        if not _close(read_back, tank.stoiip, cfg.tolerance_pct):
            mismatched.append(f"{tag} -> wrote {tank.stoiip}, read {read_back}")
    for control in cfg.controls:
        srv.set(control.tag, control.value)
        if isinstance(control.value, (int, float)):
            read_back = srv.get(control.tag)
            if not _close(read_back, float(control.value), cfg.tolerance_pct):
                mismatched.append(
                    f"{control.tag} -> wrote {control.value}, read {read_back}"
                )
    return mismatched


def run_prediction(cfg: Config, srv: Any) -> dict[str, float]:
    srv.cmd(RUN_TAG)
    return read_results(cfg, srv)


def read_results(cfg: Config, srv: Any) -> dict[str, float]:
    """Last prediction step per tank: cumulative oil, tank pressure, total."""
    out: dict[str, float] = {}
    last_step: dict[str, int] = {}
    for tank in cfg.tanks:
        tag = count_tag(cfg, tank)
        steps = int(srv.get(tag))
        if steps <= 0:
            raise RuntimeError(
                f"prediction step COUNT from {tag} must be positive, got {steps}. "
                "The prediction did not produce results."
            )
        last = steps - 1
        last_step[tank.name] = last
        out[f"np_{tank.name}"] = srv.get(oil_tag(cfg, tank, last))
        out[f"pres_{tank.name}"] = srv.get(pressure_tag(cfg, tank, last))
    out["np_total"] = sum(out[f"np_{tank.name}"] for tank in cfg.tanks)
    for result in cfg.results:
        out[result.name] = srv.get(extra_tag(cfg, result, last_step))
    return out


def _close(a: float, b: float, tolerance_pct: float) -> bool:
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    if b == 0:
        return abs(a) <= 1e-12
    return abs(a - b) / abs(b) * 100.0 <= tolerance_pct


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _fmt(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    return f"{value:,.4g}"


def compare_table(
    title: str,
    left_label: str,
    right_label: str,
    left: dict[str, float],
    right: dict[str, float],
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
        diff = b - a
        pct = diff / a * 100.0 if math.isfinite(a) and a != 0 else float("nan")
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


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_show(cfg: Config) -> int:
    print(f"model      {cfg.mbal_file}")
    print(f"tag_mode   {cfg.tag_mode}")
    print(f"tolerance  {cfg.tolerance_pct}%")
    print(f"units      {cfg.units or '(model defaults - set units: to be sure)'}")
    print("\nvolumes written by --run:")
    for tank in cfg.tanks:
        print(f"  {tank.stoiip:>12}  {stoiip_tag(cfg, tank)}")
    print("\ncontrols written by --run:")
    for control in cfg.controls or ():
        print(f"  {control.value!s:>12}  {control.tag}   ({control.name})")
    if not cfg.controls:
        print("  (none)")
    print("\nresults read after each prediction:")
    for tank in cfg.tanks:
        print(f"  count      {count_tag(cfg, tank)}")
        print(f"  np         {oil_tag(cfg, tank, 0).replace('[0].OIL', '[k].OIL')}")
        print(
            f"  pressure   "
            f"{pressure_tag(cfg, tank, 0).replace('[0].TANK', '[k].TANK')}"
        )
    for result in cfg.results:
        print(f"  {result.name:<10} {result.tag}")
    if not cfg.units:
        print(
            "\nNo units: set. MBAL then answers in the model's current unit "
            "set, which is the most common reason Python and the GUI disagree."
        )
    return 0


def cmd_check(cfg: Config, srv: Any) -> int:
    open_model(cfg, srv)
    in_model = read_volumes(cfg, srv)
    in_yaml = {tank.name: tank.stoiip for tank in cfg.tanks}
    table, ok = compare_table(
        "STOIIP: what MBAL holds vs what this YAML would write",
        "in MBAL",
        "in YAML",
        in_model,
        in_yaml,
        cfg.tolerance_pct,
    )
    print(table)
    if ok:
        print("Volumes match. --run will not change the model's STOIIP.")
        return 0
    print(
        "Volumes differ. Either the YAML numbers are not the official ones, or "
        "the units differ (set units: stoiip: ...). Fix this before comparing "
        "prediction results."
    )
    return 1


def cmd_baseline(cfg: Config, srv: Any) -> dict[str, float]:
    """The official run: reload the model, predict, write nothing."""
    open_model(cfg, srv)
    volumes = read_volumes(cfg, srv)
    start = time.time()
    results = run_prediction(cfg, srv)
    elapsed = time.time() - start
    print(f"baseline prediction ran in {elapsed:.1f}s (no inputs written)")
    write_row(cfg, "baseline", {**{f"stoiip_{k}": v for k, v in volumes.items()},
                                **results, "runtime_s": round(elapsed, 2)})
    return results


def cmd_run(cfg: Config, srv: Any) -> dict[str, float]:
    """Write the YAML volumes and controls, then predict."""
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
    inputs = {f"stoiip_{tank.name}": tank.stoiip for tank in cfg.tanks}
    inputs.update({control.name: control.value for control in cfg.controls})
    write_row(cfg, "run", {**inputs, **results, "runtime_s": round(elapsed, 2)})
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
        "volumes already differ, the YAML is not at the official numbers. If "
        "they match, a control in the YAML is changing the prediction."
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
        help="read the model's STOIIP and compare it with the YAML; no writes",
    )
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="run the prediction exactly as saved; writes nothing",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="write the YAML volumes and controls, then run the prediction",
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
