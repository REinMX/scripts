"""
Shared core for probabilistic MBAL via Petroleum Experts OpenServer.

Used by the thin entry scripts:
  - probabilistic_mbal_openserver.py
  - probabilistic_mbal_openserver_gas_lift.py

Supports independent per-tank STOIIP sampling (MC/LHS), optional aquifer
parameters, optional gas-lift sensitivity sweeps, YAML config, resume-safe
CSV output, tag validation, logging, and P90/P50/P10 summaries with plots.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

LOG = logging.getLogger("mbal")

# -----------------------------------------------------------------------------
# 1. CONFIGURATION
# -----------------------------------------------------------------------------

DistributionKind = Literal["fixed", "uniform", "triangular", "lognormal"]
SamplingMethod = Literal["lhs", "mc"]
TagMode = Literal["index", "name"]


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

    def to_dict(self) -> dict[str, Any]:
        data = {"kind": self.kind}
        for key in ("value", "low", "mode", "high", "p90", "p10"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class TankConfig:
    """Configuration for one independently sampled MBAL tank."""

    key: str
    name: str
    index: int
    stoiip: Distribution
    aquifer_multiplier: Distribution | None = None
    aquifer_volume: Distribution | None = None


def _default_index_tanks() -> tuple[TankConfig, ...]:
    """Example priors for index-based OpenServer tags."""
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


def _default_name_tanks() -> tuple[TankConfig, ...]:
    """Example priors for name-based OpenServer tags (gas-lift workflow)."""
    return (
        TankConfig(
            key="bottom",
            name="REPLACE_WITH_BOTTOM_TANK_NAME",
            index=0,
            stoiip=Distribution(kind="lognormal", p90=20.0, p10=70.0),
            aquifer_volume=None,
        ),
        TankConfig(
            key="top",
            name="REPLACE_WITH_TOP_TANK_NAME",
            index=1,
            stoiip=Distribution(kind="triangular", low=15.0, mode=45.0, high=90.0),
            aquifer_volume=None,
        ),
    )


DEFAULT_INDEX_TAGS: dict[str, str] = {
    "tank_stoiip": 'MBAL.MB[0].TANK[{i}].OIIP("{u}")',
    "aquifer_mult": "MBAL.MB[0].TANK[{i}].AQUIFER.VOLRATIO",
    "cmd_open": 'MBAL.OPENFILE("{path}")',
    "cmd_run_pred": "MBAL.MB[0].PREDICTION.CALCULATE",
    "cmd_close": "MBAL.SHUTDOWN",
    "res_nsteps": "MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT",
    "res_cumoil": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
}

DEFAULT_NAME_TAGS: dict[str, str] = {
    "tank_stoiip": "MBAL.MB[0].TANK[{tank}].OOIP",
    "aquifer_volume": "MBAL.MB[0].TANK[{tank}].AQUIFVOLUME",
    "gas_lift_rate": "MBAL.MB[0].PREDWELL[{well}][{p}].GASLIFTRATE",
    "cmd_open": 'MBAL.OPENFILE("{path}")',
    "cmd_run_pred": "MBAL.MB[0].PREDICTION.CALCULATE",
    "cmd_close": "MBAL.SHUTDOWN",
    "res_nsteps": "MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT",
    "res_cumoil": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
}


@dataclass(frozen=True)
class Config:
    # --- model -------------------------------------------------------------
    mbal_file: str = r"C:\Work\Models\two_tank_model.mbi"
    tanks: tuple[TankConfig, ...] = field(default_factory=_default_index_tanks)
    openserver_prog_id: str = "PX32.OpenServer.1"
    unit_stoiip: str = "MMstb"
    unit_press: str = "psig"
    unit_cum: str = "MMstb"
    tag_mode: str = "index"  # index | name
    tags: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INDEX_TAGS))

    # --- gas lift (optional) -----------------------------------------------
    gas_lift_well: str = "REPLACE_WITH_GAS_LIFT_WELL_NAME"
    gas_lift_prediction_index: int = 1
    gas_lift_values: tuple[float, ...] = ()

    # --- Monte Carlo -------------------------------------------------------
    n_realizations: int = 200
    seed: int = 42
    sampling: str = "lhs"  # lhs or mc

    # --- run control -------------------------------------------------------
    out_csv: str = "mbal_mc_results.csv"
    out_dir: str = "mbal_mc_output"
    stop_on_error: bool = False
    validate_tags: bool = True
    reconnect_every: int = 0  # 0 = never reconnect; N = reopen model every N ok runs
    log_level: str = "INFO"
    extra_percentiles: tuple[float, ...] = (5.0, 95.0)  # reported as P95/P5 (O&G)


def default_config(*, gas_lift: bool = False) -> Config:
    """Return the canned default Config for either workflow."""
    if gas_lift:
        return Config(
            tanks=_default_name_tanks(),
            tag_mode="name",
            tags=dict(DEFAULT_NAME_TAGS),
            out_csv="mbal_gas_lift_results.csv",
            out_dir="mbal_gas_lift_output",
        )
    return Config()


# -----------------------------------------------------------------------------
# 2. YAML / DICT CONFIG LOADING
# -----------------------------------------------------------------------------


def _parse_distribution(raw: Any, label: str) -> Distribution:
    if not isinstance(raw, dict):
        raise TypeError(f"{label}: distribution must be a mapping")
    kind = str(raw.get("kind", "")).lower()
    if not kind:
        raise ValueError(f"{label}: missing distribution kind")
    return Distribution(
        kind=kind,
        value=_optional_float(raw.get("value")),
        low=_optional_float(raw.get("low")),
        mode=_optional_float(raw.get("mode")),
        high=_optional_float(raw.get("high")),
        p90=_optional_float(raw.get("p90")),
        p10=_optional_float(raw.get("p10")),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _parse_tank(raw: dict[str, Any], position: int) -> TankConfig:
    if "key" not in raw:
        raise ValueError(f"tank[{position}]: missing key")
    key = str(raw["key"])
    name = str(raw.get("name", key))
    index = int(raw.get("index", position))
    if "stoiip" not in raw:
        raise ValueError(f"tank[{position}] ({key}): missing stoiip")
    aquifer_multiplier = None
    if raw.get("aquifer_multiplier") is not None:
        aquifer_multiplier = _parse_distribution(
            raw["aquifer_multiplier"], f"tank {key} aquifer_multiplier"
        )
    aquifer_volume = None
    if raw.get("aquifer_volume") is not None:
        aquifer_volume = _parse_distribution(
            raw["aquifer_volume"], f"tank {key} aquifer_volume"
        )
    return TankConfig(
        key=key,
        name=name,
        index=index,
        stoiip=_parse_distribution(raw["stoiip"], f"tank {key} STOIIP"),
        aquifer_multiplier=aquifer_multiplier,
        aquifer_volume=aquifer_volume,
    )


def config_from_dict(data: dict[str, Any], *, base: Config | None = None) -> Config:
    """Build a Config from a plain dict (e.g. loaded YAML)."""
    cfg = base if base is not None else default_config(gas_lift=False)
    updates: dict[str, Any] = {}

    simple = (
        "mbal_file",
        "openserver_prog_id",
        "unit_stoiip",
        "unit_press",
        "unit_cum",
        "tag_mode",
        "gas_lift_well",
        "out_csv",
        "out_dir",
        "sampling",
        "log_level",
    )
    for key in simple:
        if key in data and data[key] is not None:
            updates[key] = data[key]

    int_keys = (
        "n_realizations",
        "seed",
        "gas_lift_prediction_index",
        "reconnect_every",
    )
    for key in int_keys:
        if key in data and data[key] is not None:
            updates[key] = int(data[key])

    # aliases
    if "n" in data and data["n"] is not None and "n_realizations" not in updates:
        updates["n_realizations"] = int(data["n"])
    if "model" in data and data["model"] is not None and "mbal_file" not in updates:
        updates["mbal_file"] = str(data["model"])

    bool_keys = ("stop_on_error", "validate_tags")
    for key in bool_keys:
        if key in data and data[key] is not None:
            updates[key] = bool(data[key])

    if "gas_lift_values" in data and data["gas_lift_values"] is not None:
        raw_values = data["gas_lift_values"]
        if isinstance(raw_values, str):
            values = tuple(
                float(part.strip())
                for part in raw_values.split(",")
                if part.strip()
            )
        else:
            values = tuple(float(v) for v in raw_values)
        updates["gas_lift_values"] = values

    if "extra_percentiles" in data and data["extra_percentiles"] is not None:
        updates["extra_percentiles"] = tuple(
            float(v) for v in data["extra_percentiles"]
        )

    tag_mode = str(updates.get("tag_mode", cfg.tag_mode)).lower()
    if "tags" in data and data["tags"] is not None:
        if not isinstance(data["tags"], dict):
            raise ValueError("tags must be a mapping of name -> OpenServer string")
        base_tags = (
            dict(DEFAULT_NAME_TAGS)
            if tag_mode == "name"
            else dict(DEFAULT_INDEX_TAGS)
        )
        # Prefer existing cfg tags if already customized, else mode defaults.
        merged = dict(cfg.tags) if cfg.tags else base_tags
        if tag_mode != cfg.tag_mode:
            merged = base_tags
        merged.update({str(k): str(v) for k, v in data["tags"].items()})
        updates["tags"] = merged
    elif tag_mode != cfg.tag_mode:
        updates["tags"] = (
            dict(DEFAULT_NAME_TAGS) if tag_mode == "name" else dict(DEFAULT_INDEX_TAGS)
        )

    if "tanks" in data and data["tanks"] is not None:
        if not isinstance(data["tanks"], list) or not data["tanks"]:
            raise ValueError("tanks must be a non-empty list")
        updates["tanks"] = tuple(
            _parse_tank(item, i) for i, item in enumerate(data["tanks"])
        )

    return replace(cfg, **updates)


def load_config_yaml(path: str | Path, *, base: Config | None = None) -> Config:
    """Load Config from a YAML file."""
    try:
        import yaml
    except ImportError as error:
        raise ImportError(
            "PyYAML is required for --config; install with: pip install pyyaml"
        ) from error

    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise TypeError(f"config file {path} must contain a mapping at the top level")
    return config_from_dict(data, base=base)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Serialize Config to a YAML-friendly dict."""
    tanks = []
    for tank in cfg.tanks:
        item: dict[str, Any] = {
            "key": tank.key,
            "name": tank.name,
            "index": tank.index,
            "stoiip": tank.stoiip.to_dict(),
        }
        if tank.aquifer_multiplier is not None:
            item["aquifer_multiplier"] = tank.aquifer_multiplier.to_dict()
        if tank.aquifer_volume is not None:
            item["aquifer_volume"] = tank.aquifer_volume.to_dict()
        tanks.append(item)

    return {
        "mbal_file": cfg.mbal_file,
        "tag_mode": cfg.tag_mode,
        "openserver_prog_id": cfg.openserver_prog_id,
        "unit_stoiip": cfg.unit_stoiip,
        "unit_press": cfg.unit_press,
        "unit_cum": cfg.unit_cum,
        "n_realizations": cfg.n_realizations,
        "seed": cfg.seed,
        "sampling": cfg.sampling,
        "out_dir": cfg.out_dir,
        "out_csv": cfg.out_csv,
        "stop_on_error": cfg.stop_on_error,
        "validate_tags": cfg.validate_tags,
        "reconnect_every": cfg.reconnect_every,
        "log_level": cfg.log_level,
        "gas_lift_well": cfg.gas_lift_well,
        "gas_lift_prediction_index": cfg.gas_lift_prediction_index,
        "gas_lift_values": list(cfg.gas_lift_values),
        "extra_percentiles": list(cfg.extra_percentiles),
        "tanks": tanks,
        "tags": dict(cfg.tags),
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
    if cfg.tag_mode.lower() not in {"index", "name"}:
        raise ValueError("tag_mode must be 'index' or 'name'")
    if not cfg.tanks:
        raise ValueError("at least one tank must be configured")
    if cfg.reconnect_every < 0:
        raise ValueError("reconnect_every must be >= 0")

    keys = [tank.key for tank in cfg.tanks]
    indices = [tank.index for tank in cfg.tanks]
    if len(keys) != len(set(keys)):
        raise ValueError("tank keys must be unique")
    if len(indices) != len(set(indices)):
        raise ValueError("tank indices must be unique")

    required_tags = {"cmd_open", "cmd_run_pred", "cmd_close", "tank_stoiip"}
    missing_tags = required_tags.difference(cfg.tags)
    if missing_tags:
        raise ValueError(f"tags missing required keys: {sorted(missing_tags)}")

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
        if tank.aquifer_volume is not None:
            validate_distribution(
                tank.aquifer_volume, f"tank {tank.key} aquifer volume"
            )
    if any(value < 0.0 or not math.isfinite(value) for value in cfg.gas_lift_values):
        raise ValueError("gas-lift sensitivity values must be finite and >= 0")
    if cfg.gas_lift_values and "gas_lift_rate" not in cfg.tags:
        raise ValueError("gas_lift_values set but tags['gas_lift_rate'] is missing")


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

    distributions: list[Distribution] = [tank.stoiip for tank in cfg.tanks]
    distributions.extend(
        tank.aquifer_multiplier
        for tank in cfg.tanks
        if tank.aquifer_multiplier is not None
    )
    distributions.extend(
        tank.aquifer_volume for tank in cfg.tanks if tank.aquifer_volume is not None
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
        if tank.aquifer_volume is not None:
            data[f"aquifer_volume_{tank.key}"] = draw(tank.aquifer_volume)

    samples = pd.DataFrame(data)
    if cfg.gas_lift_values:
        blocks = []
        for gas_lift_rate in cfg.gas_lift_values:
            block = samples.copy()
            block["base_realization"] = block["realization"]
            block["gas_lift_rate"] = float(gas_lift_rate)
            blocks.append(block)
        samples = pd.concat(blocks, ignore_index=True)
        samples["realization"] = np.arange(len(samples), dtype=int)
    return samples


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
        self.prog_id = prog_id

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

    def try_get(self, tag: str) -> float | None:
        """Return float value or None if the tag is not readable."""
        try:
            return self.get(tag)
        except (RuntimeError, TypeError, ValueError):
            return None

    def shutdown(self, close_tag: str) -> None:
        with suppress(Exception):
            self.cmd(close_tag)

    def __enter__(self) -> OpenServer:  # noqa: PYI034 - keep Python 3.10 support
        return self

    def __exit__(self, *_exc: object) -> None:
        # Best-effort; callers that own the close tag should call shutdown().
        with suppress(Exception):
            self.cmd('MBAL.SHUTDOWN')


# -----------------------------------------------------------------------------
# 5. TAG FORMATTING, VALIDATION, REALIZATION I/O
# -----------------------------------------------------------------------------


def _stoiip_tag(cfg: Config, tank: TankConfig) -> str:
    template = cfg.tags["tank_stoiip"]
    if cfg.tag_mode.lower() == "name":
        return template.format(tank=tank.name, i=tank.index, u=cfg.unit_stoiip)
    return template.format(i=tank.index, tank=tank.name, u=cfg.unit_stoiip)


def _aquifer_mult_tag(cfg: Config, tank: TankConfig) -> str:
    template = cfg.tags["aquifer_mult"]
    return template.format(i=tank.index, tank=tank.name)


def _aquifer_volume_tag(cfg: Config, tank: TankConfig) -> str:
    template = cfg.tags["aquifer_volume"]
    return template.format(tank=tank.name, i=tank.index)


def _gas_lift_tag(cfg: Config) -> str:
    return cfg.tags["gas_lift_rate"].format(
        well=cfg.gas_lift_well, p=cfg.gas_lift_prediction_index
    )


def input_tags_for_validation(cfg: Config) -> list[tuple[str, str]]:
    """Return (label, tag) pairs that should be readable/writable after open."""
    pairs: list[tuple[str, str]] = []
    for tank in cfg.tanks:
        pairs.append((f"STOIIP/{tank.key}", _stoiip_tag(cfg, tank)))
        if tank.aquifer_multiplier is not None and "aquifer_mult" in cfg.tags:
            pairs.append(
                (f"aquifer_mult/{tank.key}", _aquifer_mult_tag(cfg, tank))
            )
        if tank.aquifer_volume is not None and "aquifer_volume" in cfg.tags:
            pairs.append(
                (f"aquifer_volume/{tank.key}", _aquifer_volume_tag(cfg, tank))
            )
    if cfg.gas_lift_values and "gas_lift_rate" in cfg.tags:
        pairs.append(("gas_lift_rate", _gas_lift_tag(cfg)))
    return pairs


def validate_openserver_tags(srv: OpenServer, cfg: Config) -> None:
    """Fail fast if configured input tags are not readable after model open."""
    failures: list[str] = []
    for label, tag in input_tags_for_validation(cfg):
        value = srv.try_get(tag)
        if value is None:
            failures.append(f"  - {label}: {tag}")
        else:
            LOG.info("tag ok %-20s = %s  (%s)", label, value, tag)
    if failures:
        joined = "\n".join(failures)
        raise RuntimeError(
            "OpenServer tag validation failed. Copy exact variable names from "
            "MBAL's OpenServer browser into config tags / TAGS defaults:\n"
            f"{joined}"
        )


def apply_realization(srv: SetServer, row: pd.Series, cfg: Config) -> None:
    """Write each tank's independently sampled inputs into MBAL."""
    for tank in cfg.tanks:
        srv.set(_stoiip_tag(cfg, tank), float(row[f"stoiip_{tank.key}"]))
        if tank.aquifer_multiplier is not None:
            if "aquifer_mult" not in cfg.tags:
                raise KeyError("tank has aquifer_multiplier but tags lack aquifer_mult")
            srv.set(
                _aquifer_mult_tag(cfg, tank),
                float(row[f"aq_mult_{tank.key}"]),
            )
        if tank.aquifer_volume is not None:
            if "aquifer_volume" not in cfg.tags:
                raise KeyError("tank has aquifer_volume but tags lack aquifer_volume")
            srv.set(
                _aquifer_volume_tag(cfg, tank),
                float(row[f"aquifer_volume_{tank.key}"]),
            )

    if "gas_lift_rate" in row.index and pd.notna(row["gas_lift_rate"]):
        srv.set(_gas_lift_tag(cfg), float(row["gas_lift_rate"]))


def read_results(srv: OpenServer, cfg: Config, row: pd.Series) -> dict[str, float]:
    """Read the last prediction state for every configured tank."""
    output: dict[str, float] = {}
    for tank in cfg.tanks:
        try:
            n_steps = int(srv.get(cfg.tags["res_nsteps"].format(i=tank.index)))
            last = max(n_steps - 1, 0)
        except (RuntimeError, TypeError, ValueError, KeyError):
            # Some IPM versions expose only the final state at index 0.
            last = 0

        cumulative_oil = srv.get(
            cfg.tags["res_cumoil"].format(i=tank.index, k=last, u=cfg.unit_cum)
        )
        pressure = srv.get(
            cfg.tags["res_pressure"].format(i=tank.index, k=last, u=cfg.unit_press)
        )
        try:
            cumulative_water = srv.get(
                cfg.tags["res_cumwat"].format(i=tank.index, k=last, u=cfg.unit_cum)
            )
        except (RuntimeError, KeyError):
            cumulative_water = float("nan")

        stoiip = float(row[f"stoiip_{tank.key}"])
        output[f"np_{tank.key}"] = cumulative_oil
        output[f"pres_{tank.key}"] = pressure
        output[f"wp_{tank.key}"] = cumulative_water
        if stoiip == 0.0 or not math.isfinite(stoiip):
            output[f"rf_{tank.key}"] = float("nan")
        else:
            output[f"rf_{tank.key}"] = cumulative_oil / stoiip

    output["np_total"] = sum(output[f"np_{tank.key}"] for tank in cfg.tanks)
    stoiip_total = float(row["stoiip_total"])
    if stoiip_total == 0.0 or not math.isfinite(stoiip_total):
        output["rf_total"] = float("nan")
    else:
        output["rf_total"] = output["np_total"] / stoiip_total
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
    columns.extend(
        f"aquifer_volume_{tank.key}"
        for tank in cfg.tanks
        if tank.aquifer_volume is not None
    )
    if cfg.gas_lift_values:
        columns.extend(["base_realization", "gas_lift_rate"])
    return columns


def _completed_realizations(
    csv_path: str, samples: pd.DataFrame, cfg: Config
) -> set[int]:
    """Load resume state; only successful rows skip re-run; failures are retried."""
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
    known_ids = {int(value) for value in previous.index}
    unknown = known_ids.difference(int(value) for value in expected.index)
    if unknown:
        raise RuntimeError(
            f"cannot resume {csv_path}: it contains realization IDs outside "
            "the current sample table"
        )

    for realization in known_ids:
        for column in _input_columns(cfg):
            old = float(previous.at[realization, column])
            new = float(expected.at[realization, column])
            if not np.isclose(old, new, rtol=1e-10, atol=1e-12):
                raise RuntimeError(
                    f"cannot resume {csv_path}: realization {realization} "
                    f"has a different {column}; seed/distributions changed"
                )

    if "status" in previous.columns:
        ok_mask = previous["status"].astype(str) == "ok"
        completed = {int(idx) for idx in previous.index[ok_mask]}
        failed = known_ids - completed
        if failed:
            LOG.info(
                "Resume: %d ok, %d failed will be retried",
                len(completed),
                len(failed),
            )
        return completed

    # Legacy CSVs without status: treat presence as complete.
    return known_ids


def _format_eta(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "?"
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _open_model(cfg: Config) -> OpenServer:
    srv = OpenServer(cfg.openserver_prog_id)
    model_path = cfg.mbal_file.replace('"', '\\"')
    srv.cmd(cfg.tags["cmd_open"].format(path=model_path))
    LOG.info("Opened %s", cfg.mbal_file)
    if cfg.validate_tags:
        validate_openserver_tags(srv, cfg)
    return srv


def run_monte_carlo(samples: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    os.makedirs(cfg.out_dir, exist_ok=True)
    csv_path = os.path.join(cfg.out_dir, cfg.out_csv)
    completed = _completed_realizations(csv_path, samples, cfg)
    pending = [
        int(row.realization)
        for row in samples.itertuples(index=False)
        if int(row.realization) not in completed
    ]
    total_pending = len(pending)
    if completed:
        LOG.info(
            "Resuming: %d already ok in %s; %d remaining",
            len(completed),
            csv_path,
            total_pending,
        )

    srv = _open_model(cfg)
    ok_since_reconnect = 0
    done_this_session = 0
    runtimes: list[float] = []
    session_start = time.time()

    try:
        for _, row in samples.iterrows():
            realization = int(row["realization"])
            if realization in completed:
                continue

            record = new_result_record(row, cfg)
            start = time.time()
            try:
                apply_realization(srv, row, cfg)
                srv.slow_cmd(cfg.tags["cmd_run_pred"])
                record.update(read_results(srv, cfg, row))
                record["status"] = "ok"
            except Exception as error:
                record["status"] = f"failed: {error}"
                LOG.exception("[%s] FAILED", realization)
                if cfg.stop_on_error:
                    raise
            elapsed = time.time() - start
            record["runtime_s"] = round(elapsed, 2)

            # Drop any prior failed row for this realization before appending ok/fail.
            _append_result_row(csv_path, record, realization)

            done_this_session += 1
            if record["status"] == "ok":
                ok_since_reconnect += 1
                runtimes.append(elapsed)
                volumes = ", ".join(
                    f"{tank.key}={row[f'stoiip_{tank.key}']:.1f}" for tank in cfg.tanks
                )
                production = ", ".join(
                    f"Np_{tank.key}={record[f'np_{tank.key}']:.2f}"
                    for tank in cfg.tanks
                )
                remaining = total_pending - done_this_session
                avg = float(np.mean(runtimes)) if runtimes else float("nan")
                eta = _format_eta(avg * remaining)
                LOG.info(
                    "[%s] STOIIP %s -> %s (%.2fs)  %d/%d remaining, ETA %s",
                    realization,
                    volumes,
                    production,
                    elapsed,
                    remaining,
                    total_pending,
                    eta,
                )

                if (
                    cfg.reconnect_every
                    and ok_since_reconnect >= cfg.reconnect_every
                ):
                    LOG.info(
                        "Reconnecting OpenServer after %d successful runs",
                        ok_since_reconnect,
                    )
                    srv.shutdown(cfg.tags["cmd_close"])
                    srv = _open_model(cfg)
                    ok_since_reconnect = 0
    finally:
        srv.shutdown(cfg.tags["cmd_close"])
        LOG.info(
            "Session finished in %s",
            _format_eta(time.time() - session_start),
        )

    return pd.read_csv(csv_path)


def _append_result_row(
    csv_path: str, record: dict[str, object], realization: int
) -> None:
    """Append a result row; rewrite file without prior rows for the same id."""
    if os.path.exists(csv_path):
        previous = pd.read_csv(csv_path)
        if "realization" in previous.columns:
            previous = previous[previous["realization"] != realization]
            if previous.empty:
                # Rewrite cleanly with header for the new single row.
                pd.DataFrame([record]).to_csv(csv_path, index=False)
                return
            previous.to_csv(csv_path, index=False)
            pd.DataFrame([record]).to_csv(csv_path, mode="a", header=False, index=False)
            return
    pd.DataFrame([record]).to_csv(csv_path, index=False)


# -----------------------------------------------------------------------------
# 6. SUMMARY AND PLOTS
# -----------------------------------------------------------------------------


def percentiles(
    values: np.ndarray, *, extra: tuple[float, ...] = (5.0, 95.0)
) -> dict[str, float]:
    """O&G percentiles: P90 = 10th, P50 = 50th, P10 = 90th, plus optional extras.

    Extra values are statistical percentiles; labels use the O&G convention
    P{100-p} for p in extra when p is 5 or 95 (so 5 -> P95, 95 -> P5).
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    base = {"P90": np.nan, "P50": np.nan, "P10": np.nan, "mean": np.nan, "std": np.nan}
    for p in extra:
        base[_extra_percentile_label(p)] = np.nan
    if values.size == 0:
        return base
    result = {
        "P90": float(np.percentile(values, 10)),
        "P50": float(np.percentile(values, 50)),
        "P10": float(np.percentile(values, 90)),
        "mean": float(values.mean()),
        "std": float(values.std(ddof=0)),
    }
    for p in extra:
        result[_extra_percentile_label(p)] = float(np.percentile(values, p))
    return result


def _extra_percentile_label(stat_percentile: float) -> str:
    """Map a statistical percentile to an O&G-style label when conventional."""
    # O&G: P95 is the low (5th statistical), P5 is the high (95th statistical).
    mapping = {5.0: "P95", 95.0: "P5"}
    if stat_percentile in mapping:
        return mapping[stat_percentile]
    # Fallback: Pn where n is the O&G exceedance-style label 100-p.
    oil_label = 100.0 - float(stat_percentile)
    if oil_label == int(oil_label):
        return f"P{int(oil_label)}"
    return f"P{oil_label:g}"


def _summary_columns(
    cfg: Config, df: pd.DataFrame, *, include_prediction_results: bool = True
) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    columns.extend(
        (f"stoiip_{tank.key}", f"{tank.name} STOIIP [{cfg.unit_stoiip}]")
        for tank in cfg.tanks
    )
    columns.append(("stoiip_total", f"Field STOIIP [{cfg.unit_stoiip}]"))

    if not include_prediction_results:
        return columns

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
    LOG.info("wrote %s", path)


def _summarize_gas_lift(df: pd.DataFrame, cfg: Config) -> None:
    """Write and plot field-oil sensitivity versus deterministic gas-lift rate."""
    if "gas_lift_rate" not in df.columns or "np_total" not in df.columns:
        return
    successful = df[df["status"] == "ok"] if "status" in df.columns else df
    if successful.empty:
        return

    rows = []
    for gas_lift_rate, group in successful.groupby("gas_lift_rate", sort=True):
        stats = percentiles(
            group["np_total"].to_numpy(dtype=float), extra=cfg.extra_percentiles
        )
        rows.append({"gas_lift_rate": gas_lift_rate, **stats, "n": len(group)})
    sensitivity = pd.DataFrame(rows)
    path = os.path.join(cfg.out_dir, "gas_lift_sensitivity.csv")
    sensitivity.to_csv(path, index=False)
    LOG.info("wrote %s", path)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    figure, axes = plt.subplots(figsize=(7, 4))
    x = sensitivity["gas_lift_rate"].to_numpy(dtype=float)
    for column in ("P90", "P50", "P10", "mean"):
        if column in sensitivity.columns:
            axes.plot(x, sensitivity[column], marker="o", label=column)
    axes.set_xlabel("Gas lift rate [current MBAL model units]")
    axes.set_ylabel(f"Field cumulative oil [{cfg.unit_cum}]")
    axes.set_title("Gas-lift sensitivity")
    axes.grid(alpha=0.3)
    axes.legend()
    figure.tight_layout()
    plot_path = os.path.join(cfg.out_dir, "gas_lift_sensitivity.png")
    figure.savefig(plot_path, dpi=130)
    plt.close(figure)
    LOG.info("wrote %s", plot_path)


def _plot_field_total_stoiip(df: pd.DataFrame, cfg: Config) -> None:
    if "stoiip_total" not in df.columns:
        return
    values = df["stoiip_total"].dropna().to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    figure, axes = plt.subplots(1, 2, figsize=(11, 4))
    axes[0].hist(values, bins=30, color="steelblue", alpha=0.8)
    axes[0].set_title("Field STOIIP (sum of independent tanks)")
    axes[0].set_xlabel(cfg.unit_stoiip)
    axes[0].set_ylabel("count")
    ordered = np.sort(values)
    exceedance = (ordered.size - np.arange(1, ordered.size + 1) + 0.5) / ordered.size
    axes[1].plot(ordered, exceedance, color="steelblue")
    axes[1].set_title("Field STOIIP exceedance")
    axes[1].set_xlabel(cfg.unit_stoiip)
    axes[1].set_ylabel("P(X > x)")
    axes[1].grid(alpha=0.3)
    for probability in (0.9, 0.5, 0.1):
        axes[1].axhline(probability, linestyle=":", linewidth=0.8, color="grey")
    figure.tight_layout()
    path = os.path.join(cfg.out_dir, "stoiip_field_total.png")
    figure.savefig(path, dpi=130)
    plt.close(figure)
    LOG.info("wrote %s", path)


def summarize(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    successful = df[df["status"] == "ok"] if "status" in df.columns else df
    n_ok = len(successful)
    n_all = len(df)
    n_fail = (
        int((df["status"].astype(str) != "ok").sum())
        if "status" in df.columns
        else 0
    )
    LOG.info("%d successful / %d total (%d failed or incomplete)", n_ok, n_all, n_fail)
    print(f"\n{n_ok} successful realizations of {n_all}\n")

    gas_lift_sweep = "gas_lift_rate" in df.columns and bool(cfg.gas_lift_values)
    # Also treat presence of gas_lift_rate column from prior runs as sweep mode.
    if "gas_lift_rate" in df.columns and "base_realization" in df.columns:
        gas_lift_sweep = True

    if gas_lift_sweep:
        # Geological inputs are repeated once per lift setting. Summarize each
        # base realization once, and leave prediction metrics to the
        # per-rate gas_lift_sensitivity.csv table.
        if "base_realization" in df.columns:
            summary_source = df.drop_duplicates("base_realization", keep="first")
        else:
            summary_source = successful
    else:
        summary_source = successful

    rows = []
    for column, label in _summary_columns(
        cfg,
        summary_source,
        include_prediction_results=not gas_lift_sweep,
    ):
        if column not in summary_source.columns:
            continue
        stats = percentiles(
            summary_source[column].to_numpy(), extra=cfg.extra_percentiles
        )
        rows.append(
            {
                "variable": label,
                **{key: round(value, 3) for key, value in stats.items()},
            }
        )
    summary = pd.DataFrame(rows)
    if not summary.empty:
        print(summary.to_string(index=False))

    os.makedirs(cfg.out_dir, exist_ok=True)
    summary_path = os.path.join(cfg.out_dir, "summary_percentiles.csv")
    summary.to_csv(summary_path, index=False)
    LOG.info("wrote %s", summary_path)

    # Run metadata
    meta = {
        "n_rows": n_all,
        "n_ok": n_ok,
        "n_failed": n_fail,
        "seed": cfg.seed,
        "sampling": cfg.sampling,
        "n_tanks": len(cfg.tanks),
        "gas_lift_values": list(cfg.gas_lift_values),
    }
    meta_path = os.path.join(cfg.out_dir, "run_metadata.csv")
    pd.DataFrame([meta]).to_csv(meta_path, index=False)
    LOG.info("wrote %s", meta_path)

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        LOG.warning("matplotlib not installed - skipping plots")
        if gas_lift_sweep:
            _summarize_gas_lift(df, cfg)
        return summary

    _plot_tank_metric(
        summary_source,
        cfg,
        prefix="stoiip",
        title="Independent STOIIP distributions per tank",
        x_label=cfg.unit_stoiip,
        filename="stoiip_per_tank.png",
    )
    _plot_field_total_stoiip(summary_source, cfg)
    if not gas_lift_sweep:
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
    _summarize_gas_lift(df, cfg)
    return summary


# -----------------------------------------------------------------------------
# 7. LOGGING AND CLI
# -----------------------------------------------------------------------------


def setup_logging(level: str = "INFO", *, log_file: str | None = None) -> None:
    """Configure the mbal logger (idempotent-ish for CLI use)."""
    root = logging.getLogger("mbal")
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def build_arg_parser(
    description: str,
    *,
    gas_lift_cli: bool = False,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        help="YAML config file (tanks, tags, model path, run controls)",
    )
    parser.add_argument("--n", type=int, help="number of realizations")
    parser.add_argument("--model", help="path to the MBAL .mbi file")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--sampling", choices=("lhs", "mc"))
    parser.add_argument("--out-dir")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--no-validate-tags",
        action="store_true",
        help="skip OpenServer input-tag probe after opening the model",
    )
    parser.add_argument(
        "--reconnect-every",
        type=int,
        help="reopen MBAL every N successful realizations (0 = never)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="abort the run on the first OpenServer failure",
    )
    if gas_lift_cli:
        parser.add_argument(
            "--gas-lift-values",
            help=(
                "comma-separated deterministic gas-lift sensitivity values in the "
                "current MBAL model units, e.g. 0,0.5,1.0,1.5"
            ),
        )
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
    parser.add_argument(
        "--write-example-config",
        metavar="PATH",
        help="write an example YAML config to PATH and exit",
    )
    return parser


def apply_cli_overrides(
    cfg: Config,
    args: argparse.Namespace,
    *,
    gas_lift_cli: bool = False,
) -> Config:
    updates: dict[str, Any] = {}
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
    if args.log_level:
        updates["log_level"] = args.log_level
    if args.no_validate_tags:
        updates["validate_tags"] = False
    if args.reconnect_every is not None:
        updates["reconnect_every"] = args.reconnect_every
    if args.stop_on_error:
        updates["stop_on_error"] = True
    if gas_lift_cli and getattr(args, "gas_lift_values", None):
        try:
            updates["gas_lift_values"] = tuple(
                float(value.strip())
                for value in args.gas_lift_values.split(",")
                if value.strip()
            )
        except ValueError as error:
            raise SystemExit(f"invalid --gas-lift-values: {error}") from error
    return replace(cfg, **updates) if updates else cfg


def write_example_config(path: str, *, gas_lift: bool = False) -> None:
    try:
        import yaml
    except ImportError as error:
        raise SystemExit(
            "PyYAML is required to write example config; pip install pyyaml"
        ) from error

    cfg = default_config(gas_lift=gas_lift)
    if gas_lift:
        cfg = replace(cfg, gas_lift_values=(0.0, 0.5, 1.0, 1.5))
    data = config_to_dict(cfg)
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
    print(f"wrote example config to {path}")


def main(
    argv: list[str] | None = None,
    *,
    gas_lift: bool = False,
    description: str | None = None,
) -> int:
    """CLI entry used by both thin wrapper scripts."""
    desc = description or (
        "Probabilistic MBAL via OpenServer "
        + ("(gas-lift sensitivity)" if gas_lift else "(independent per-tank sampling)")
    )
    parser = build_arg_parser(desc, gas_lift_cli=gas_lift)
    args = parser.parse_args(argv)

    if args.write_example_config:
        write_example_config(args.write_example_config, gas_lift=gas_lift)
        return 0

    cfg = default_config(gas_lift=gas_lift)
    if args.config:
        cfg = load_config_yaml(args.config, base=cfg)
    cfg = apply_cli_overrides(cfg, args, gas_lift_cli=gas_lift)
    validate_config(cfg)

    log_path = os.path.join(cfg.out_dir, "mbal_run.log")
    setup_logging(cfg.log_level, log_file=None)  # file after out_dir known
    # Always log to stdout; also to out_dir when not dry-run only is fine always
    os.makedirs(cfg.out_dir, exist_ok=True)
    setup_logging(cfg.log_level, log_file=log_path)

    if args.summarize_only:
        path = os.path.join(cfg.out_dir, cfg.out_csv)
        LOG.info("Summarize-only from %s", path)
        summarize(pd.read_csv(path), cfg)
        return 0

    samples = build_sample_table(cfg)
    LOG.info(
        "Sampled %d rows (%s, seed=%s, base n=%s)",
        len(samples),
        cfg.sampling.upper(),
        cfg.seed,
        cfg.n_realizations,
    )
    print(
        f"Sampled {len(samples)} realizations "
        f"({cfg.sampling.upper()}, seed={cfg.seed})"
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
            "\nSample STOIIP rank correlation "
            "(independent target: approximately zero):"
        )
        rank_correlation = samples[stoiip_columns].rank().corr()
        print(rank_correlation.round(3).to_string())

    if args.dry_run:
        path = os.path.join(cfg.out_dir, "samples_dry_run.csv")
        samples.to_csv(path, index=False)
        LOG.info("Dry run - wrote %s. No MBAL session opened.", path)
        print(f"\nDry run - wrote {path}. No MBAL session was opened.")
        summarize(samples, cfg)
        return 0

    if sys.platform != "win32":
        LOG.error("OpenServer is Windows-only. Use --dry-run elsewhere.")
        print("OpenServer is Windows-only. Use --dry-run elsewhere.", file=sys.stderr)
        return 1

    results = run_monte_carlo(samples, cfg)
    summarize(results, cfg)
    return 0


# Back-compat module-level defaults used by older tests/scripts.
TAGS = dict(DEFAULT_INDEX_TAGS)
CFG = default_config(gas_lift=False)

__all__ = [
    "CFG",
    "DEFAULT_INDEX_TAGS",
    "DEFAULT_NAME_TAGS",
    "TAGS",
    "Config",
    "Distribution",
    "OpenServer",
    "SetServer",
    "TankConfig",
    "apply_realization",
    "build_sample_table",
    "config_from_dict",
    "config_to_dict",
    "default_config",
    "load_config_yaml",
    "lognormal_from_p90_p10",
    "main",
    "new_result_record",
    "percentiles",
    "read_results",
    "run_monte_carlo",
    "sample_distribution",
    "setup_logging",
    "summarize",
    "unit_hypercube",
    "validate_config",
    "validate_distribution",
    "validate_openserver_tags",
]
