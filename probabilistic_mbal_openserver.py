"""
Probabilistic MBAL via Petroleum Experts OpenServer
===================================================

The MBAL model may contain any number of tanks. Each tank has its OWN STOIIP
distribution and is sampled from its OWN independent Monte Carlo / Latin
Hypercube dimension. There is no shared total-STOIIP rank and no sampled split
fraction:

    STOIIP_A     ~ Tank A distribution
    STOIIP_B     ~ Tank B distribution
    STOIIP_total = STOIIP_A + STOIIP_B       # derived after sampling

This avoids the artificial dependence created by sampling one field total and
then forcing the tanks to share it through a split fraction. Optional aquifer
multipliers are also configured and sampled independently for each tank.

For every realization the script:
    1. writes every tank's STOIIP and optional aquifer multiplier into MBAL,
    2. runs the MBAL prediction,
    3. reads per-tank cumulative oil, pressure, water and recovery factor,
    4. appends one row to a CSV so a stopped run can safely resume.

Finally it reports O&G P90 / P50 / P10 per tank and for the field total, and
writes comparison plots.

IMPORTANT — OPENSERVER TAG STRINGS
----------------------------------
The exact OpenServer variable strings for MBAL change between IPM versions.
DO NOT trust the defaults in the TAGS block below blindly. Get the real ones from
MBAL itself: open the model, navigate to the field you want, and use the
OpenServer variable browser / right-click "Copy variable name". Paste the exact
strings for your version into TAGS.

Usage
-----
    python probabilistic_mbal_openserver.py --dry-run
    python probabilistic_mbal_openserver.py --n 500
    python probabilistic_mbal_openserver.py --model C:\\Models\\field.mbi
    python probabilistic_mbal_openserver.py --summarize-only

Requires: numpy, pandas, matplotlib, (scipy optional), pywin32 (Windows only).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Protocol

import numpy as np
import pandas as pd

# -----------------------------------------------------------------------------
# 1. CONFIGURATION
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Distribution:
    """One scalar input distribution.

    Supported forms:
      fixed:      value
      uniform:    low, high
      triangular: low, mode, high
      lognormal:  p90 (low case), p10 (high case), using O&G convention
    """

    kind: str
    value: float | None = None
    low: float | None = None
    mode: float | None = None
    high: float | None = None
    p90: float | None = None
    p10: float | None = None


@dataclass(frozen=True)
class TankConfig:
    """Configuration for one independently sampled MBAL tank."""

    key: str
    name: str
    index: int
    stoiip: Distribution
    aquifer_multiplier: Distribution | None = None


def _default_tanks() -> tuple[TankConfig, ...]:
    """Example priors only — replace these with the asset-specific ranges."""
    return (
        TankConfig(
            key="A",
            name="Tank A",
            index=0,
            stoiip=Distribution(kind="lognormal", p90=20.0, p10=70.0),
        ),
        TankConfig(
            key="B",
            name="Tank B",
            index=1,
            stoiip=Distribution(kind="triangular", low=15.0, mode=45.0, high=90.0),
        ),
    )


@dataclass(frozen=True)
class Config:
    # --- model -------------------------------------------------------------
    mbal_file: str = r"C:\Work\Models\two_tank_model.mbi"
    tanks: tuple[TankConfig, ...] = field(default_factory=_default_tanks)
    openserver_prog_id: str = "PX32.OpenServer.1"
    unit_stoiip: str = "MMstb"
    unit_press: str = "psig"
    unit_cum: str = "MMstb"

    # --- Monte Carlo -------------------------------------------------------
    n_realizations: int = 200
    seed: int = 42
    sampling: str = "lhs"  # lhs or mc

    # --- run control -------------------------------------------------------
    out_csv: str = "mbal_mc_results.csv"
    out_dir: str = "mbal_mc_output"
    stop_on_error: bool = False


CFG = Config()

# -----------------------------------------------------------------------------
# 2. OPENSERVER TAG STRINGS  <-- VERIFY AGAINST YOUR IPM VERSION
# -----------------------------------------------------------------------------
# {i} is the MBAL tank index, {k} is the result timestep and {u} is the unit.

TAGS = {
    # inputs
    "tank_stoiip": 'MBAL.MB[0].TANK[{i}].OIIP("{u}")',
    "aquifer_mult": "MBAL.MB[0].TANK[{i}].AQUIFER.VOLRATIO",
    # commands
    "cmd_open": 'MBAL.OPENFILE("{path}")',
    "cmd_run_pred": "MBAL.MB[0].PREDICTION.CALCULATE",
    "cmd_close": "MBAL.SHUTDOWN",
    # outputs
    "res_nsteps": "MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT",
    "res_cumoil": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
}

# -----------------------------------------------------------------------------
# 3. VALIDATION AND SAMPLING
# -----------------------------------------------------------------------------

_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SUPPORTED_DISTRIBUTIONS = {"fixed", "uniform", "triangular", "lognormal"}


def _required_number(value: float | None, label: str) -> float:
    if value is None or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def validate_distribution(
    dist: Distribution, label: str, *, positive: bool = True
) -> None:
    """Validate one distribution before any samples or MBAL calls are made."""
    kind = dist.kind.lower()
    if kind not in _SUPPORTED_DISTRIBUTIONS:
        allowed = ", ".join(sorted(_SUPPORTED_DISTRIBUTIONS))
        raise ValueError(f"{label}: unknown distribution {dist.kind!r}; use {allowed}")

    lower_bound: float
    if kind == "fixed":
        lower_bound = _required_number(dist.value, f"{label} value")
    elif kind == "lognormal":
        p90 = _required_number(dist.p90, f"{label} p90")
        p10 = _required_number(dist.p10, f"{label} p10")
        if p90 >= p10:
            raise ValueError(f"{label}: p90 must be lower than p10")
        lower_bound = p90
    elif kind == "uniform":
        low = _required_number(dist.low, f"{label} low")
        high = _required_number(dist.high, f"{label} high")
        if low >= high:
            raise ValueError(f"{label}: low must be lower than high")
        lower_bound = low
    else:
        low = _required_number(dist.low, f"{label} low")
        mode = _required_number(dist.mode, f"{label} mode")
        high = _required_number(dist.high, f"{label} high")
        if not low <= mode <= high or low >= high:
            raise ValueError(f"{label}: require low <= mode <= high and low < high")
        lower_bound = low

    if positive and lower_bound <= 0.0:
        raise ValueError(f"{label}: values must be greater than zero")


def validate_config(cfg: Config) -> None:
    """Validate tank identity, distributions and run controls."""
    if cfg.n_realizations <= 0:
        raise ValueError("n_realizations must be greater than zero")
    if cfg.sampling.lower() not in {"lhs", "mc"}:
        raise ValueError("sampling must be 'lhs' or 'mc'")
    if not cfg.tanks:
        raise ValueError("at least one tank must be configured")

    keys = [tank.key for tank in cfg.tanks]
    indices = [tank.index for tank in cfg.tanks]
    if len(keys) != len(set(keys)):
        raise ValueError("tank keys must be unique")
    if len(indices) != len(set(indices)):
        raise ValueError("tank indices must be unique")

    for tank in cfg.tanks:
        if not _KEY_PATTERN.fullmatch(tank.key):
            raise ValueError(
                f"tank key {tank.key!r} must start with a letter and contain only "
                "letters, numbers and underscores"
            )
        if tank.index < 0:
            raise ValueError(f"tank {tank.key}: index must be non-negative")
        validate_distribution(tank.stoiip, f"tank {tank.key} STOIIP")
        if tank.aquifer_multiplier is not None:
            validate_distribution(
                tank.aquifer_multiplier, f"tank {tank.key} aquifer multiplier"
            )


def lognormal_from_p90_p10(p90: float, p10: float) -> tuple[float, float]:
    """Return mu and sigma of ln(X) matching O&G low P90 and high P10."""
    z = 1.2815515655446004
    sigma = math.log(p10 / p90) / (2.0 * z)
    mu = 0.5 * math.log(p10 * p90)
    return mu, sigma


def _norm_ppf(u: np.ndarray) -> np.ndarray:
    try:
        from scipy.stats import norm

        return norm.ppf(u)
    except ImportError:
        # Acklam rational approximation; sufficient for input sampling.
        a = [
            -3.969683028665376e01,
            2.209460984245205e02,
            -2.759285104469687e02,
            1.383577518672690e02,
            -3.066479806614716e01,
            2.506628277459239e00,
        ]
        b = [
            -5.447609879822406e01,
            1.615858368580409e02,
            -1.556989798598866e02,
            6.680131188771972e01,
            -1.328068155288572e01,
        ]
        c = [
            -7.784894002430293e-03,
            -3.223964580411365e-01,
            -2.400758277161838e00,
            -2.549732539343734e00,
            4.374664141464968e00,
            2.938163982698783e00,
        ]
        d = [
            7.784695709041462e-03,
            3.224671290700398e-01,
            2.445134137142996e00,
            3.754408661907416e00,
        ]
        plow, phigh = 0.02425, 1.0 - 0.02425
        u = np.clip(u, 1e-12, 1.0 - 1e-12)
        out = np.empty_like(u)
        lo, hi = u < plow, u > phigh
        mid = ~(lo | hi)

        q = np.sqrt(-2.0 * np.log(u[lo]))
        out[lo] = (
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        q = np.sqrt(-2.0 * np.log(1.0 - u[hi]))
        out[hi] = -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
        q = u[mid] - 0.5
        r = q * q
        out[mid] = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
        return out


def _tri_ppf(u: np.ndarray, low: float, mode: float, high: float) -> np.ndarray:
    c = (mode - low) / (high - low)
    return np.where(
        u < c,
        low + np.sqrt(np.maximum(u * (high - low) * (mode - low), 0.0)),
        high - np.sqrt(np.maximum((1.0 - u) * (high - low) * (high - mode), 0.0)),
    )


def unit_hypercube(n: int, dimensions: int, cfg: Config) -> np.ndarray:
    """Return independent U(0,1) dimensions using MC or randomized LHS."""
    if dimensions == 0:
        return np.empty((n, 0), dtype=float)

    rng = np.random.default_rng(cfg.seed)
    if cfg.sampling.lower() == "lhs":
        try:
            from scipy.stats.qmc import LatinHypercube

            return LatinHypercube(d=dimensions, seed=cfg.seed).random(n)
        except ImportError:
            values = np.empty((n, dimensions), dtype=float)
            for column in range(dimensions):
                permutation = rng.permutation(n)
                values[:, column] = (permutation + rng.random(n)) / n
            return values
    return rng.random((n, dimensions))


def _needs_random_dimension(dist: Distribution) -> bool:
    return dist.kind.lower() != "fixed"


def sample_distribution(dist: Distribution, u: np.ndarray | None, n: int) -> np.ndarray:
    """Transform one independent unit-hypercube column into a distribution."""
    kind = dist.kind.lower()
    if kind == "fixed":
        return np.full(
            n, _required_number(dist.value, "fixed distribution value"), dtype=float
        )
    if u is None:
        raise ValueError(f"distribution {kind!r} requires a random sample dimension")
    if kind == "uniform":
        low = _required_number(dist.low, "uniform distribution low")
        high = _required_number(dist.high, "uniform distribution high")
        return low + u * (high - low)
    if kind == "triangular":
        low = _required_number(dist.low, "triangular distribution low")
        mode = _required_number(dist.mode, "triangular distribution mode")
        high = _required_number(dist.high, "triangular distribution high")
        return _tri_ppf(u, low, mode, high)
    if kind == "lognormal":
        p90 = _required_number(dist.p90, "lognormal distribution p90")
        p10 = _required_number(dist.p10, "lognormal distribution p10")
        mu, sigma = lognormal_from_p90_p10(p90, p10)
        return np.exp(mu + sigma * _norm_ppf(u))
    raise ValueError(f"unknown distribution {dist.kind!r}")


def build_sample_table(cfg: Config) -> pd.DataFrame:
    """Sample every tank independently and derive the field total afterward."""
    validate_config(cfg)

    distributions = [tank.stoiip for tank in cfg.tanks]
    distributions.extend(
        tank.aquifer_multiplier
        for tank in cfg.tanks
        if tank.aquifer_multiplier is not None
    )
    dimensions = sum(_needs_random_dimension(dist) for dist in distributions)
    unit_samples = unit_hypercube(cfg.n_realizations, dimensions, cfg)
    next_dimension = 0

    def draw(dist: Distribution) -> np.ndarray:
        nonlocal next_dimension
        if _needs_random_dimension(dist):
            u = unit_samples[:, next_dimension]
            next_dimension += 1
        else:
            u = None
        return sample_distribution(dist, u, cfg.n_realizations)

    data: dict[str, np.ndarray] = {
        "realization": np.arange(cfg.n_realizations, dtype=int)
    }
    stoiip_columns: list[str] = []
    for tank in cfg.tanks:
        column = f"stoiip_{tank.key}"
        data[column] = draw(tank.stoiip)
        stoiip_columns.append(column)

    data["stoiip_total"] = np.sum(
        np.column_stack([data[column] for column in stoiip_columns]), axis=1
    )

    for tank in cfg.tanks:
        if tank.aquifer_multiplier is not None:
            data[f"aq_mult_{tank.key}"] = draw(tank.aquifer_multiplier)

    return pd.DataFrame(data)


# -----------------------------------------------------------------------------
# 4. OPENSERVER SESSION
# -----------------------------------------------------------------------------


class SetServer(Protocol):
    def set(self, tag: str, value: float) -> None: ...


class OpenServer:
    """Thin wrapper around the Petroleum Experts OpenServer COM object."""

    def __init__(self, prog_id: str = "PX32.OpenServer.1"):
        import win32com.client  # lazy import: dry-run works on Linux/macOS

        self.os = win32com.client.Dispatch(prog_id)

    def _check(self, code: int, what: str) -> None:
        if code:
            description = "(no description available)"
            with suppress(Exception):  # COM may fail while describing a COM failure.
                description = self.os.GetErrorDescription(code)
            raise RuntimeError(f"OpenServer error {code} on {what}: {description}")

    def cmd(self, command: str) -> None:
        self._check(self.os.DoCommand(command), command)

    def slow_cmd(self, command: str) -> None:
        """Run a calculation command that may block until MBAL completes."""
        self._check(self.os.DoSlowCommand(command), command)

    def set(self, tag: str, value: float) -> None:
        self._check(self.os.DoSet(tag, value), f"DoSet {tag}")

    def get(self, tag: str) -> float:
        value = self.os.DoGet(tag)
        error = self.os.GetLastError("MBAL")
        if error:
            raise RuntimeError(
                f"OpenServer error reading {tag}: {self.os.GetErrorDescription(error)}"
            )
        return float(value)

    def __enter__(self) -> OpenServer:  # noqa: PYI034 - keep Python 3.10 support
        return self

    def __exit__(self, *_exc: object) -> None:
        with suppress(Exception):  # Best-effort shutdown must not mask run errors.
            self.cmd(TAGS["cmd_close"])


# -----------------------------------------------------------------------------
# 5. REALIZATION EXECUTION
# -----------------------------------------------------------------------------


def apply_realization(srv: SetServer, row: pd.Series, cfg: Config) -> None:
    """Write each tank's independently sampled inputs to its MBAL index."""
    for tank in cfg.tanks:
        srv.set(
            TAGS["tank_stoiip"].format(i=tank.index, u=cfg.unit_stoiip),
            float(row[f"stoiip_{tank.key}"]),
        )
        if tank.aquifer_multiplier is not None:
            srv.set(
                TAGS["aquifer_mult"].format(i=tank.index),
                float(row[f"aq_mult_{tank.key}"]),
            )


def read_results(srv: OpenServer, cfg: Config, row: pd.Series) -> dict[str, float]:
    """Read the last prediction state for every configured tank."""
    output: dict[str, float] = {}
    for tank in cfg.tanks:
        try:
            n_steps = int(srv.get(TAGS["res_nsteps"].format(i=tank.index)))
            last = max(n_steps - 1, 0)
        except (RuntimeError, TypeError, ValueError):
            # Some IPM versions expose only the final state at index 0.
            last = 0

        cumulative_oil = srv.get(
            TAGS["res_cumoil"].format(i=tank.index, k=last, u=cfg.unit_cum)
        )
        pressure = srv.get(
            TAGS["res_pressure"].format(i=tank.index, k=last, u=cfg.unit_press)
        )
        try:
            cumulative_water = srv.get(
                TAGS["res_cumwat"].format(i=tank.index, k=last, u=cfg.unit_cum)
            )
        except RuntimeError:
            cumulative_water = float("nan")

        stoiip = float(row[f"stoiip_{tank.key}"])
        output[f"np_{tank.key}"] = cumulative_oil
        output[f"pres_{tank.key}"] = pressure
        output[f"wp_{tank.key}"] = cumulative_water
        output[f"rf_{tank.key}"] = cumulative_oil / stoiip

    output["np_total"] = sum(output[f"np_{tank.key}"] for tank in cfg.tanks)
    output["rf_total"] = output["np_total"] / float(row["stoiip_total"])
    return output


def new_result_record(row: pd.Series, cfg: Config) -> dict[str, object]:
    """Create a stable CSV row schema before the OpenServer call can fail."""
    record: dict[str, object] = row.to_dict()
    record["realization"] = int(row["realization"])
    for tank in cfg.tanks:
        for prefix in ("np", "pres", "wp", "rf"):
            record[f"{prefix}_{tank.key}"] = float("nan")
    record["np_total"] = float("nan")
    record["rf_total"] = float("nan")
    record["status"] = "not run"
    record["runtime_s"] = float("nan")
    return record


def _input_columns(cfg: Config) -> list[str]:
    columns = [f"stoiip_{tank.key}" for tank in cfg.tanks]
    columns.append("stoiip_total")
    columns.extend(
        f"aq_mult_{tank.key}"
        for tank in cfg.tanks
        if tank.aquifer_multiplier is not None
    )
    return columns


def _completed_realizations(
    csv_path: str, samples: pd.DataFrame, cfg: Config
) -> set[int]:
    """Load resume state and reject a CSV generated from different samples."""
    if not os.path.exists(csv_path):
        return set()

    previous = pd.read_csv(csv_path)
    required = {"realization", *_input_columns(cfg)}
    missing = required.difference(previous.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(
            f"cannot resume {csv_path}: missing input columns {names}; "
            "move the old CSV or use a different out_dir"
        )

    previous = previous.drop_duplicates("realization", keep="last").set_index(
        "realization"
    )
    expected = samples.set_index("realization")
    completed = {int(value) for value in previous.index}
    unknown = completed.difference(int(value) for value in expected.index)
    if unknown:
        raise RuntimeError(
            f"cannot resume {csv_path}: it contains realization IDs outside "
            "the current sample table"
        )

    for realization in completed:
        for column in _input_columns(cfg):
            old = float(previous.at[realization, column])
            new = float(expected.at[realization, column])
            if not np.isclose(old, new, rtol=1e-10, atol=1e-12):
                raise RuntimeError(
                    f"cannot resume {csv_path}: realization {realization} "
                    f"has a different {column}; seed/distributions changed"
                )
    return completed


def run_monte_carlo(samples: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    os.makedirs(cfg.out_dir, exist_ok=True)
    csv_path = os.path.join(cfg.out_dir, cfg.out_csv)
    completed = _completed_realizations(csv_path, samples, cfg)
    if completed:
        print(f"Resuming: {len(completed)} realizations already in {csv_path}")

    with OpenServer(cfg.openserver_prog_id) as srv:
        model_path = cfg.mbal_file.replace('"', '\\"')
        srv.cmd(TAGS["cmd_open"].format(path=model_path))
        print(f"Opened {cfg.mbal_file}")

        for _, row in samples.iterrows():
            realization = int(row["realization"])
            if realization in completed:
                continue

            record = new_result_record(row, cfg)
            start = time.time()
            try:
                apply_realization(srv, row, cfg)
                srv.slow_cmd(TAGS["cmd_run_pred"])
                record.update(read_results(srv, cfg, row))
                record["status"] = "ok"
            except Exception as error:
                record["status"] = f"failed: {error}"
                print(f"  [{realization}] FAILED: {error}")
                if cfg.stop_on_error:
                    raise
            record["runtime_s"] = round(time.time() - start, 2)

            pd.DataFrame([record]).to_csv(
                csv_path,
                mode="a",
                header=not os.path.exists(csv_path),
                index=False,
            )

            if record["status"] == "ok":
                volumes = ", ".join(
                    f"{tank.key}={row[f'stoiip_{tank.key}']:.1f}" for tank in cfg.tanks
                )
                production = ", ".join(
                    f"Np_{tank.key}={record[f'np_{tank.key}']:.2f}"
                    for tank in cfg.tanks
                )
                print(
                    f"  [{realization}] STOIIP {volumes} -> {production} "
                    f"({record['runtime_s']}s)"
                )

    return pd.read_csv(csv_path)


# -----------------------------------------------------------------------------
# 6. SUMMARY AND PLOTS
# -----------------------------------------------------------------------------


def percentiles(values: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"P90": np.nan, "P50": np.nan, "P10": np.nan, "mean": np.nan}
    return {
        "P90": float(np.percentile(values, 10)),
        "P50": float(np.percentile(values, 50)),
        "P10": float(np.percentile(values, 90)),
        "mean": float(values.mean()),
    }


def _summary_columns(cfg: Config, df: pd.DataFrame) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    columns.extend(
        (f"stoiip_{tank.key}", f"{tank.name} STOIIP [{cfg.unit_stoiip}]")
        for tank in cfg.tanks
    )
    columns.append(("stoiip_total", f"Field STOIIP [{cfg.unit_stoiip}]"))

    for prefix, label, unit in (
        ("np", "cumulative oil", cfg.unit_cum),
        ("pres", "pressure", cfg.unit_press),
        ("wp", "cumulative water", cfg.unit_cum),
        ("rf", "recovery factor", "fraction"),
    ):
        columns.extend(
            (
                f"{prefix}_{tank.key}",
                f"{tank.name} {label} [{unit}]",
            )
            for tank in cfg.tanks
            if f"{prefix}_{tank.key}" in df.columns
        )
        total_column = f"{prefix}_total"
        if total_column in df.columns:
            columns.append((total_column, f"Field {label} [{unit}]"))
    return columns


def _plot_tank_metric(
    df: pd.DataFrame,
    cfg: Config,
    *,
    prefix: str,
    title: str,
    x_label: str,
    filename: str,
) -> None:
    series: list[tuple[np.ndarray, str]] = []
    for tank in cfg.tanks:
        column = f"{prefix}_{tank.key}"
        if column not in df.columns:
            continue
        values = df[column].dropna().to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if values.size:
            series.append((values, tank.name))
    if not series:
        return

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(11, 4))
    for values, name in series:
        axes[0].hist(values, bins=30, alpha=0.5, label=name)
        ordered = np.sort(values)
        exceedance = (
            ordered.size - np.arange(1, ordered.size + 1) + 0.5
        ) / ordered.size
        axes[1].plot(ordered, exceedance, label=name)

    axes[0].set_title(title)
    axes[0].set_xlabel(x_label)
    axes[0].set_ylabel("count")
    axes[0].legend()
    axes[1].set_title("Exceedance: P90 / P50 / P10")
    axes[1].set_xlabel(x_label)
    axes[1].set_ylabel("P(X > x)")
    axes[1].grid(alpha=0.3)
    axes[1].legend()
    for probability in (0.9, 0.5, 0.1):
        axes[1].axhline(probability, linestyle=":", linewidth=0.8, color="grey")
    figure.tight_layout()

    path = os.path.join(cfg.out_dir, filename)
    figure.savefig(path, dpi=130)
    plt.close(figure)
    print(f"wrote {path}")


def summarize(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    successful = df[df["status"] == "ok"] if "status" in df.columns else df
    print(f"\n{len(successful)} successful realizations of {len(df)}\n")

    rows = []
    for column, label in _summary_columns(cfg, successful):
        stats = percentiles(successful[column].to_numpy())
        rows.append(
            {
                "variable": label,
                **{key: round(value, 3) for key, value in stats.items()},
            }
        )
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))

    os.makedirs(cfg.out_dir, exist_ok=True)
    summary_path = os.path.join(cfg.out_dir, "summary_percentiles.csv")
    summary.to_csv(summary_path, index=False)
    print(f"wrote {summary_path}")

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("\n(matplotlib not installed - skipping plots)")
        return summary

    _plot_tank_metric(
        successful,
        cfg,
        prefix="stoiip",
        title="Independent STOIIP distributions per tank",
        x_label=cfg.unit_stoiip,
        filename="stoiip_per_tank.png",
    )
    _plot_tank_metric(
        successful,
        cfg,
        prefix="np",
        title="Cumulative oil per tank",
        x_label=cfg.unit_cum,
        filename="cumulative_oil_per_tank.png",
    )
    _plot_tank_metric(
        successful,
        cfg,
        prefix="rf",
        title="Recovery factor per tank",
        x_label="fraction",
        filename="recovery_factor_per_tank.png",
    )
    _plot_tank_metric(
        successful,
        cfg,
        prefix="pres",
        title="Final average pressure per tank",
        x_label=cfg.unit_press,
        filename="pressure_per_tank.png",
    )
    return summary


# -----------------------------------------------------------------------------
# 7. MAIN
# -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, help="number of realizations")
    parser.add_argument("--model", help="path to the MBAL .mbi file")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--sampling", choices=("lhs", "mc"))
    parser.add_argument("--out-dir")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sample and report inputs only; never open MBAL",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="re-read the results CSV and regenerate summary/plots",
    )
    args = parser.parse_args(argv)

    updates = {}
    if args.n is not None:
        updates["n_realizations"] = args.n
    if args.model:
        updates["mbal_file"] = args.model
    if args.seed is not None:
        updates["seed"] = args.seed
    if args.sampling:
        updates["sampling"] = args.sampling
    if args.out_dir:
        updates["out_dir"] = args.out_dir
    cfg = replace(CFG, **updates)
    validate_config(cfg)

    if args.summarize_only:
        path = os.path.join(cfg.out_dir, cfg.out_csv)
        summarize(pd.read_csv(path), cfg)
        return 0

    samples = build_sample_table(cfg)
    print(
        f"Sampled {len(samples)} realizations ({cfg.sampling.upper()}, seed={cfg.seed})"
    )
    input_columns = [column for column in samples.columns if column != "realization"]
    print(
        samples[input_columns]
        .describe()
        .T[["mean", "min", "50%", "max"]]
        .round(3)
        .to_string()
    )

    stoiip_columns = [f"stoiip_{tank.key}" for tank in cfg.tanks]
    if len(stoiip_columns) > 1:
        print(
            "\nSample STOIIP rank correlation (independent target: approximately zero):"
        )
        rank_correlation = samples[stoiip_columns].rank().corr()
        print(rank_correlation.round(3).to_string())

    if args.dry_run:
        os.makedirs(cfg.out_dir, exist_ok=True)
        path = os.path.join(cfg.out_dir, "samples_dry_run.csv")
        samples.to_csv(path, index=False)
        print(f"\nDry run - wrote {path}. No MBAL session was opened.")
        summarize(samples, cfg)
        return 0

    if sys.platform != "win32":
        print("OpenServer is Windows-only. Use --dry-run elsewhere.", file=sys.stderr)
        return 1

    results = run_monte_carlo(samples, cfg)
    summarize(results, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
