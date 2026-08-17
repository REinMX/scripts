"""
Shared core for probabilistic MBAL via Petroleum Experts OpenServer.

Used by the single CLI: mbal.py

Supports:
  - connected-volume oil in place: official x connectivity x residual,
    with correlated connectivity groups and an optional upside tank
  - optional aquifer parameters
  - deterministic operational sweeps (gas lift, water-injection rate, BHP)
  - YAML config, resume-safe CSV, tag validation, P90/P50/P10 summaries

This is a dynamic material-balance model. Tank volumes are inputs to MBAL,
not a geological realization.

OpenServer well/injector names below follow Petroleum Experts, *IPM OpenServer
User Manual* (January 2011), Part 5 MBAL: PREDINP / PREDWELL. Copy the exact
strings from MBAL's browser for your IPM version before a licensed run.
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

# Keys already warned about, so a per-realization problem is reported once.
_WARNED_ONCE: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log a warning the first time this key is seen in the process."""
    if key in _WARNED_ONCE:
        return
    _WARNED_ONCE.add(key)
    LOG.warning(message, *args)

# -----------------------------------------------------------------------------
# 1. CONFIGURATION
# -----------------------------------------------------------------------------

DistributionKind = Literal["fixed", "uniform", "triangular", "lognormal"]
SamplingMethod = Literal["lhs", "mc"]
TagMode = Literal["index", "name"]
VolumeModelKind = Literal["connected_volume"]
WaterInjControl = Literal["rate", "bhp", "rate_with_bhp_limit"]
TankRole = Literal["base", "upside"]
ConnectivityKind = Literal["two_section", "optional"]


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
class Connectivity:
    """How much of the official tank volume the well actually sees.

    two_section
        Official assumes two sand sections communicate. If they do not,
        the well sees isolated_fraction of official (default 0.5).

    optional
        The tank is either fully on the well or not at all (upside sand).
        isolated_fraction should be 0.

    group
        Name of the shared geological risk that decides connectivity. Tanks
        isolated by the *same* fault or shale should share a group, so the
        draws are correlated instead of diversifying each other away. See
        volume_model.connectivity_correlation. Tanks with no group (or a
        group of their own) stay independent.
    """

    kind: str
    p_connected: float
    isolated_fraction: float = 0.0
    residual: Distribution | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "p_connected": self.p_connected,
            "isolated_fraction": self.isolated_fraction,
        }
        if self.group is not None:
            data["group"] = self.group
        if self.residual is not None:
            data["residual"] = self.residual.to_dict()
        return data


@dataclass(frozen=True)
class VolumeModel:
    """Connected-volume oil in place for a dynamic (MBAL) tank study.

    Official numbers are mapped *connected-case* volumes, not the mean.
    Discrete connectivity decides how much of that volume the well sees;
    a residual multiplier sits on top:

        STOIIP_i = official_i × connect_frac_i × residual_i
                   × (field_scale if tank.role == base)

    connect_frac is 1 or isolated_fraction (two_section), or 1 or 0
    (optional). Upside tanks are excluded from the base sum.

    connectivity_correlation
        Correlation between the connectivity draws of tanks that share a
        connectivity.group, via a one-factor Gaussian copula. 0 keeps the
        draws independent; 1 means one barrier decides them together, so
        P(all connected) = min(p_connected) rather than the product. Each
        tank's own p_connected is preserved exactly at every value.
        Independent draws are false diversification: they lift the field
        low case and invent an "exactly one sand connects" outcome.
    """

    kind: str = "connected_volume"
    field_scale: Distribution | None = None
    residual: Distribution | None = None
    max_multiplier: float | None = None
    connectivity_correlation: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "connectivity_correlation": self.connectivity_correlation,
        }
        if self.field_scale is not None:
            data["field_scale"] = self.field_scale.to_dict()
        if self.residual is not None:
            data["residual"] = self.residual.to_dict()
        if self.max_multiplier is not None:
            data["max_multiplier"] = self.max_multiplier
        return data


@dataclass(frozen=True)
class TankConfig:
    """Configuration for one MBAL tank."""

    key: str
    name: str
    index: int
    official_stoiip: float | None = None
    aquifer_multiplier: Distribution | None = None
    aquifer_volume: Distribution | None = None
    residual: Distribution | None = None
    role: str = "base"
    in_model: bool = True
    connectivity: Connectivity | None = None


def _default_exploration_tanks() -> tuple[TankConfig, ...]:
    """Working mapped volumes (MSm3). Placeholders — update when official changes.

    A: two sand sections; official 4.5 assumes they communicate.
    B: smaller sand; official 3.0 plus OWC and the same connectivity issue.
    C: deeper sand ~6.5, optional upside, not in the base case. in_model is
       false until the tank exists in the .mbi.
    """
    return (
        TankConfig(
            key="A",
            name="REPLACE_WITH_TANK_A_NAME",
            index=0,
            official_stoiip=4.5,
            role="base",
            connectivity=Connectivity(
                kind="two_section",
                p_connected=0.30,
                isolated_fraction=0.50,
                group="base_sands",
                residual=Distribution(kind="lognormal", p90=0.88, p10=1.10),
            ),
        ),
        TankConfig(
            key="B",
            name="REPLACE_WITH_TANK_B_NAME",
            index=1,
            official_stoiip=3.0,
            role="base",
            connectivity=Connectivity(
                kind="two_section",
                p_connected=0.35,
                isolated_fraction=0.50,
                group="base_sands",
                residual=Distribution(kind="lognormal", p90=0.70, p10=1.25),
            ),
        ),
        TankConfig(
            key="C",
            name="REPLACE_WITH_DEEPER_SAND_NAME",
            index=2,
            official_stoiip=6.5,
            role="upside",
            in_model=False,
            connectivity=Connectivity(
                kind="optional",
                p_connected=0.25,
                isolated_fraction=0.0,
                residual=Distribution(kind="lognormal", p90=0.60, p10=1.40),
            ),
        ),
    )


def _default_exploration_volume_model() -> VolumeModel:
    """Connected-volume prior for the sidetrack dynamic model.

    Mild shared field_scale on base tanks only (Bo / common mapping).
    Connectivity is the main discrete uncertainty; do not centre official
    as the mean.

    A and B share the 'base_sands' group: the same barrier is expected to
    decide both, so their connectivity is strongly correlated rather than
    independent. Drawing them independently would diversify the dominant
    risk away and lift the field P90. The deeper sand C is a separate
    question (presence and charge), so it stays outside the group.
    """
    return VolumeModel(
        field_scale=Distribution(kind="lognormal", p90=0.88, p10=1.10),
        connectivity_correlation=0.8,
    )


# Petex IPM OpenServer User Manual (Jan 2011) MBAL PREDWELL / PREDINP names.
# Later IPM builds expose some well constraints with a prediction-step index
# [{p}], matching the GASLIFTRATE pattern already used in this repo.
DEFAULT_INDEX_TAGS: dict[str, str] = {
    "tank_stoiip": 'MBAL.MB[0].TANK[{i}].OIIP("{u}")',
    "aquifer_mult": "MBAL.MB[0].TANK[{i}].AQUIFER.VOLRATIO",
    "gas_lift_rate": "MBAL.MB[0].PREDWELL[{i}][{p}].GASLIFTRATE",
    "water_inj_rate": "MBAL.MB[0].PREDWELL[{i}][{p}].MAXRATE",
    "water_inj_min_rate": "MBAL.MB[0].PREDWELL[{i}][{p}].MINRATE",
    "water_inj_bhp": "MBAL.MB[0].PREDWELL[{i}].CONSTFBHP",
    "water_inj_max_fbhp": "MBAL.MB[0].PREDWELL[{i}].MAXFBHP",
    "water_inj_perform": "MBAL.MB[0].PREDWELL[{i}].PERFORMTYPE",
    "pred_watinj": "MBAL.MB[0].PREDINP.WATINJ",
    "pred_max_inj_wat": "MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MAXINJWATRATE",
    "cmd_open": 'MBAL.OPENFILE("{path}")',
    "cmd_run_pred": "MBAL.MB[0].PREDICTION.CALCULATE",
    "cmd_close": "MBAL.SHUTDOWN",
    "res_nsteps": "MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT",
    "res_cumoil": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
    "res_cumwatinj": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATINJ("{u}")',
}

DEFAULT_NAME_TAGS: dict[str, str] = {
    "tank_stoiip": "MBAL.MB[0].TANK[{tank}].OOIP",
    "aquifer_volume": "MBAL.MB[0].TANK[{tank}].AQUIFVOLUME",
    "gas_lift_rate": "MBAL.MB[0].PREDWELL[{well}][{p}].GASLIFTRATE",
    "water_inj_rate": "MBAL.MB[0].PREDWELL[{well}][{p}].MAXRATE",
    "water_inj_min_rate": "MBAL.MB[0].PREDWELL[{well}][{p}].MINRATE",
    "water_inj_bhp": "MBAL.MB[0].PREDWELL[{well}].CONSTFBHP",
    "water_inj_max_fbhp": "MBAL.MB[0].PREDWELL[{well}].MAXFBHP",
    "water_inj_perform": "MBAL.MB[0].PREDWELL[{well}].PERFORMTYPE",
    "pred_watinj": "MBAL.MB[0].PREDINP.WATINJ",
    "pred_max_inj_wat": "MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MAXINJWATRATE",
    "cmd_open": 'MBAL.OPENFILE("{path}")',
    "cmd_run_pred": "MBAL.MB[0].PREDICTION.CALCULATE",
    "cmd_close": "MBAL.SHUTDOWN",
    "res_nsteps": "MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT",
    "res_cumoil": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
    "res_cumwatinj": 'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATINJ("{u}")',
}


@dataclass(frozen=True)
class Config:
    # --- model -------------------------------------------------------------
    mbal_file: str = r"C:\Work\Models\two_tank_model.mbi"
    tanks: tuple[TankConfig, ...] = field(
        default_factory=_default_exploration_tanks
    )
    openserver_prog_id: str = "PX32.OpenServer.1"
    unit_stoiip: str = "MSm3"
    unit_press: str = "bara"
    unit_cum: str = "MSm3"
    tag_mode: str = "name"  # index | name
    tags: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_NAME_TAGS))

    # --- volume uncertainty -----------------------------------------------
    volume_model: VolumeModel = field(
        default_factory=_default_exploration_volume_model
    )

    # --- gas lift (optional) -----------------------------------------------
    gas_lift_well: str = "REPLACE_WITH_GAS_LIFT_WELL_NAME"
    gas_lift_well_index: int = 0  # used by tag_mode 'index' ({i} in the tag)
    gas_lift_prediction_index: int = 1
    gas_lift_values: tuple[float, ...] = ()

    # --- water injector (optional) ----------------------------------------
    # One WATINJ well linked to both tanks in the .mbi; we set well rate/BHP
    # and MBAL allocates between tanks from injectivity / pressure.
    water_inj_well: str = "REPLACE_WITH_WATER_INJ_WELL_NAME"
    water_inj_well_index: int = 0
    water_inj_prediction_index: int = 1
    water_inj_rate_values: tuple[float, ...] = ()
    water_inj_bhp_values: tuple[float, ...] = ()
    water_inj_control: str = "rate_with_bhp_limit"

    # --- Monte Carlo -------------------------------------------------------
    n_realizations: int = 200
    seed: int = 42
    sampling: str = "lhs"  # lhs or mc

    # --- run control -------------------------------------------------------
    out_csv: str = "mbal_results.csv"
    out_dir: str = "mbal_output"
    stop_on_error: bool = False
    validate_tags: bool = True
    reconnect_every: int = 0  # 0 = never reconnect; N = reopen model every N ok runs
    log_level: str = "INFO"
    extra_percentiles: tuple[float, ...] = (5.0, 95.0)  # reported as P95/P5 (O&G)
    min_tank_stoiip: float = 0.01  # floor written to MBAL when sampled volume is 0


def default_config(
    *,
    gas_lift_values: tuple[float, ...] = (),
    water_inj_rate_values: tuple[float, ...] = (),
    water_inj_bhp_values: tuple[float, ...] = (),
) -> Config:
    """This well: three tanks, connected-volume prior.

    Config() already carries the well. This only fills the optional sweep
    lists, which are empty unless YAML or the CLI sets them.
    """
    return Config(
        gas_lift_values=gas_lift_values,
        water_inj_rate_values=water_inj_rate_values,
        water_inj_bhp_values=water_inj_bhp_values,
    )


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
    if raw.get("stoiip") is not None:
        raise ValueError(
            f"tank {key}: per-tank 'stoiip' distributions are no longer "
            "supported. Give official_stoiip plus connectivity instead - see "
            "docs/oil-in-place.md"
        )
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
    residual = None
    if raw.get("residual") is not None:
        residual = _parse_distribution(raw["residual"], f"tank {key} residual")
    official = _optional_float(raw.get("official_stoiip"))
    if official is None:
        raise ValueError(f"tank[{position}] ({key}): missing official_stoiip")
    connectivity = None
    if raw.get("connectivity") is not None:
        connectivity = _parse_connectivity(
            raw["connectivity"], f"tank {key} connectivity"
        )
    in_model = True
    if "in_model" in raw and raw["in_model"] is not None:
        in_model = bool(raw["in_model"])
    role = str(raw.get("role", "base")).lower()
    return TankConfig(
        key=key,
        name=name,
        index=index,
        aquifer_multiplier=aquifer_multiplier,
        aquifer_volume=aquifer_volume,
        official_stoiip=official,
        residual=residual,
        role=role,
        in_model=in_model,
        connectivity=connectivity,
    )


def _parse_connectivity(raw: Any, label: str) -> Connectivity:
    if not isinstance(raw, dict):
        raise TypeError(f"{label} must be a mapping")
    residual = None
    if raw.get("residual") is not None:
        residual = _parse_distribution(raw["residual"], f"{label} residual")
    if raw.get("p_connected") is None:
        raise ValueError(f"{label}: missing p_connected")
    isolated = raw.get("isolated_fraction")
    group = raw.get("group")
    return Connectivity(
        kind=str(raw.get("kind", "")).lower(),
        p_connected=float(raw["p_connected"]),
        isolated_fraction=0.0 if isolated is None else float(isolated),
        group=None if group is None else str(group),
        residual=residual,
    )


def _parse_volume_model(raw: Any) -> VolumeModel:
    if not isinstance(raw, dict):
        raise TypeError("volume_model must be a mapping")
    field_scale = None
    if raw.get("field_scale") is not None:
        field_scale = _parse_distribution(raw["field_scale"], "field_scale")
    residual = None
    if raw.get("residual") is not None:
        residual = _parse_distribution(raw["residual"], "volume residual")
    correlation = raw.get("connectivity_correlation")
    return VolumeModel(
        kind=str(raw.get("kind", "connected_volume")).lower(),
        field_scale=field_scale,
        residual=residual,
        max_multiplier=_optional_float(raw.get("max_multiplier")),
        connectivity_correlation=0.0 if correlation is None else float(correlation),
    )


def _parse_float_tuple(raw: Any, label: str) -> tuple[float, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        try:
            return tuple(
                float(part.strip()) for part in raw.split(",") if part.strip()
            )
        except ValueError as error:
            raise ValueError(f"invalid {label}: {error}") from error
    try:
        return tuple(float(v) for v in raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def config_from_dict(data: dict[str, Any], *, base: Config | None = None) -> Config:
    """Build a Config from a plain dict (e.g. loaded YAML)."""
    cfg = base if base is not None else default_config()
    updates: dict[str, Any] = {}

    simple = (
        "mbal_file",
        "openserver_prog_id",
        "unit_stoiip",
        "unit_press",
        "unit_cum",
        "tag_mode",
        "gas_lift_well",
        "water_inj_well",
        "water_inj_control",
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
        "gas_lift_well_index",
        "gas_lift_prediction_index",
        "water_inj_well_index",
        "water_inj_prediction_index",
        "reconnect_every",
    )
    for key in int_keys:
        if key in data and data[key] is not None:
            updates[key] = int(data[key])

    if "min_tank_stoiip" in data and data["min_tank_stoiip"] is not None:
        updates["min_tank_stoiip"] = float(data["min_tank_stoiip"])

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
        updates["gas_lift_values"] = _parse_float_tuple(
            data["gas_lift_values"], "gas_lift_values"
        )
    if "water_inj_rate_values" in data and data["water_inj_rate_values"] is not None:
        updates["water_inj_rate_values"] = _parse_float_tuple(
            data["water_inj_rate_values"], "water_inj_rate_values"
        )
    if "water_inj_bhp_values" in data and data["water_inj_bhp_values"] is not None:
        updates["water_inj_bhp_values"] = _parse_float_tuple(
            data["water_inj_bhp_values"], "water_inj_bhp_values"
        )
    if "volume_model" in data and data["volume_model"] is not None:
        updates["volume_model"] = _parse_volume_model(data["volume_model"])

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
        }
        if tank.official_stoiip is not None:
            item["official_stoiip"] = tank.official_stoiip
        if tank.residual is not None:
            item["residual"] = tank.residual.to_dict()
        if tank.role != "base":
            item["role"] = tank.role
        if not tank.in_model:
            item["in_model"] = False
        if tank.connectivity is not None:
            item["connectivity"] = tank.connectivity.to_dict()
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
        "volume_model": cfg.volume_model.to_dict(),
        "gas_lift_well": cfg.gas_lift_well,
        "gas_lift_well_index": cfg.gas_lift_well_index,
        "gas_lift_prediction_index": cfg.gas_lift_prediction_index,
        "gas_lift_values": list(cfg.gas_lift_values),
        "water_inj_well": cfg.water_inj_well,
        "water_inj_well_index": cfg.water_inj_well_index,
        "water_inj_prediction_index": cfg.water_inj_prediction_index,
        "water_inj_rate_values": list(cfg.water_inj_rate_values),
        "water_inj_bhp_values": list(cfg.water_inj_bhp_values),
        "water_inj_control": cfg.water_inj_control,
        "min_tank_stoiip": cfg.min_tank_stoiip,
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

    required_tags = {
        "cmd_open",
        "cmd_run_pred",
        "cmd_close",
        "tank_stoiip",
        "res_nsteps",
        "res_cumoil",
        "res_pressure",
    }
    missing_tags = required_tags.difference(cfg.tags)
    if missing_tags:
        raise ValueError(f"tags missing required keys: {sorted(missing_tags)}")

    _validate_volume_model(cfg)

    for tank in cfg.tanks:
        if not _KEY_PATTERN.fullmatch(tank.key):
            raise ValueError(
                f"tank key {tank.key!r} must start with a letter and contain only "
                "letters, numbers and underscores"
            )
        if tank.index < 0:
            raise ValueError(f"tank {tank.key}: index must be non-negative")
        # The tag defaults are split by mode - index mode ships aquifer_mult,
        # name mode ships aquifer_volume. Catch the mismatch here rather than
        # letting every realization fail one at a time inside the run loop.
        if tank.aquifer_multiplier is not None:
            validate_distribution(
                tank.aquifer_multiplier, f"tank {tank.key} aquifer multiplier"
            )
            if "aquifer_mult" not in cfg.tags:
                raise ValueError(
                    f"tank {tank.key}: aquifer_multiplier is set but "
                    "tags['aquifer_mult'] is missing; copy the exact string "
                    "from MBAL's OpenServer browser into tags"
                )
        if tank.aquifer_volume is not None:
            validate_distribution(
                tank.aquifer_volume, f"tank {tank.key} aquifer volume"
            )
            if "aquifer_volume" not in cfg.tags:
                raise ValueError(
                    f"tank {tank.key}: aquifer_volume is set but "
                    "tags['aquifer_volume'] is missing; copy the exact string "
                    "from MBAL's OpenServer browser into tags"
                )
    if any(value < 0.0 or not math.isfinite(value) for value in cfg.gas_lift_values):
        raise ValueError("gas-lift sensitivity values must be finite and >= 0")
    if cfg.gas_lift_values and "gas_lift_rate" not in cfg.tags:
        raise ValueError("gas_lift_values set but tags['gas_lift_rate'] is missing")
    if cfg.gas_lift_well_index < 0:
        raise ValueError("gas_lift_well_index must be non-negative")
    if cfg.gas_lift_prediction_index < 0:
        raise ValueError("gas_lift_prediction_index must be non-negative")
    _validate_water_inj(cfg)


_EXAMPLE_MBAL_FILE = r"C:\Work\Models\two_tank_model.mbi"


def _is_example_value(value: str) -> bool:
    return "REPLACE_WITH_" in value.upper()


def validate_licensed_run_config(
    cfg: Config, *, check_model_file: bool = False
) -> None:
    """Reject public example values before any licensed OpenServer action.

    This is deliberately separate from :func:`validate_config`: dry runs and
    tests must remain usable with the committed anonymized example. It cannot
    prove that a version-specific OpenServer string is correct; only a probe
    against the installed MBAL model can do that.
    """
    validate_config(cfg)
    problems: list[str] = []

    if not cfg.mbal_file.strip() or cfg.mbal_file == _EXAMPLE_MBAL_FILE:
        problems.append("mbal_file still contains the committed example path")
    elif _is_example_value(cfg.mbal_file):
        problems.append("mbal_file contains REPLACE_WITH_ placeholder text")
    elif not cfg.mbal_file.lower().endswith(".mbi"):
        problems.append("mbal_file must point to an .mbi file")
    elif check_model_file and not os.path.isfile(cfg.mbal_file):
        problems.append(f"mbal_file does not exist: {cfg.mbal_file}")

    if not cfg.openserver_prog_id.strip() or _is_example_value(
        cfg.openserver_prog_id
    ):
        problems.append("openserver_prog_id is empty or unresolved")

    for tank in _tanks_in_model(cfg):
        if not tank.name.strip() or _is_example_value(tank.name):
            problems.append(f"tank {tank.key} name is empty or unresolved")

    if cfg.gas_lift_values and (
        not cfg.gas_lift_well.strip() or _is_example_value(cfg.gas_lift_well)
    ):
        problems.append("gas_lift_well is empty or unresolved")

    if (cfg.water_inj_rate_values or cfg.water_inj_bhp_values) and (
        not cfg.water_inj_well.strip() or _is_example_value(cfg.water_inj_well)
    ):
        problems.append("water_inj_well is empty or unresolved")

    for key, value in cfg.tags.items():
        if not value.strip() or _is_example_value(value):
            problems.append(f"tags[{key!r}] is empty or unresolved")

    for key, value in (
        ("unit_stoiip", cfg.unit_stoiip),
        ("unit_press", cfg.unit_press),
        ("unit_cum", cfg.unit_cum),
    ):
        if not value.strip() or _is_example_value(value):
            problems.append(f"{key} is empty or unresolved")

    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise ValueError(
            "licensed-run config contains example/placeholder values:\n" + details
        )


def _tank_residual(cfg: Config, tank: TankConfig) -> Distribution | None:
    if tank.connectivity is not None and tank.connectivity.residual is not None:
        return tank.connectivity.residual
    if tank.residual is not None:
        return tank.residual
    return cfg.volume_model.residual


def _tanks_in_model(cfg: Config) -> tuple[TankConfig, ...]:
    return tuple(tank for tank in cfg.tanks if tank.in_model)


def _tanks_with_role(cfg: Config, role: str) -> tuple[TankConfig, ...]:
    return tuple(tank for tank in cfg.tanks if tank.role.lower() == role)


def _correlated_connectivity_groups(cfg: Config) -> tuple[str, ...]:
    """Connectivity groups whose draws are actually shared, in tank order.

    A group is live only when the correlation is positive and at least two
    tanks in it still have an uncertain p_connected. Anything else leaves
    the per-tank draws untouched, so existing seeds reproduce exactly.
    """
    if cfg.volume_model.connectivity_correlation <= 0.0:
        return ()
    counts: dict[str, int] = {}
    for tank in cfg.tanks:
        conn = tank.connectivity
        if conn is None or conn.group is None:
            continue
        if 0.0 < conn.p_connected < 1.0:
            counts[conn.group] = counts.get(conn.group, 0) + 1
    return tuple(name for name, count in counts.items() if count > 1)


def _validate_volume_model(cfg: Config) -> None:
    model = cfg.volume_model
    kind = model.kind.lower()
    if kind != "connected_volume":
        raise ValueError(
            f"volume_model.kind must be 'connected_volume' (got {model.kind!r}). "
            "The 'independent' and 'fmu_residual' models were removed - give "
            "each tank official_stoiip plus connectivity instead"
        )
    if model.max_multiplier is not None and (
        not math.isfinite(model.max_multiplier) or model.max_multiplier <= 0.0
    ):
        raise ValueError("volume_model.max_multiplier must be finite and > 0")
    if not math.isfinite(model.connectivity_correlation) or not (
        0.0 <= model.connectivity_correlation <= 1.0
    ):
        raise ValueError(
            "volume_model.connectivity_correlation must be in [0, 1]"
        )
    if cfg.min_tank_stoiip < 0.0 or not math.isfinite(cfg.min_tank_stoiip):
        raise ValueError("min_tank_stoiip must be finite and >= 0")

    for tank in cfg.tanks:
        if tank.role.lower() not in {"base", "upside"}:
            raise ValueError(f"tank {tank.key}: role must be 'base' or 'upside'")

    if model.field_scale is not None:
        validate_distribution(model.field_scale, "field_scale")
    for tank in cfg.tanks:
        if tank.official_stoiip is None or tank.official_stoiip <= 0.0:
            raise ValueError(
                f"tank {tank.key}: connected_volume requires official_stoiip > 0"
            )
        if tank.connectivity is None:
            raise ValueError(
                f"tank {tank.key}: connected_volume requires connectivity"
            )
        _validate_connectivity(tank.connectivity, f"tank {tank.key} connectivity")
        residual = _tank_residual(cfg, tank)
        if residual is None:
            raise ValueError(
                f"tank {tank.key}: connected_volume requires a residual "
                "on connectivity, the tank, or volume_model"
            )
        validate_distribution(residual, f"tank {tank.key} residual")

    grouped = any(
        tank.connectivity is not None and tank.connectivity.group is not None
        for tank in cfg.tanks
    )
    if model.connectivity_correlation > 0.0 and not _correlated_connectivity_groups(cfg):
        LOG.warning(
            "connectivity_correlation is set but no connectivity.group is shared by "
            "two tanks with an uncertain p_connected — draws stay independent."
        )
    elif grouped and model.connectivity_correlation <= 0.0:
        LOG.warning(
            "Tanks share a connectivity.group but connectivity_correlation is 0, so "
            "the shared barrier is drawn independently per tank. That is false "
            "diversification and it lifts the field low case (P90)."
        )


def _validate_connectivity(conn: Connectivity, label: str) -> None:
    if conn.kind not in {"two_section", "optional"}:
        raise ValueError(f"{label}: kind must be 'two_section' or 'optional'")
    if conn.group is not None and not conn.group.strip():
        raise ValueError(f"{label}: group must be a non-empty name or omitted")
    if not math.isfinite(conn.p_connected) or not 0.0 <= conn.p_connected <= 1.0:
        raise ValueError(f"{label}: p_connected must be in [0, 1]")
    if not math.isfinite(conn.isolated_fraction) or not (
        0.0 <= conn.isolated_fraction <= 1.0
    ):
        raise ValueError(f"{label}: isolated_fraction must be in [0, 1]")
    if conn.kind == "optional" and conn.isolated_fraction != 0.0:
        raise ValueError(f"{label}: optional connectivity requires isolated_fraction=0")


def _validate_water_inj(cfg: Config) -> None:
    control = cfg.water_inj_control.lower()
    if control not in {"rate", "bhp", "rate_with_bhp_limit"}:
        raise ValueError(
            "water_inj_control must be 'rate', 'bhp', or 'rate_with_bhp_limit'"
        )
    if any(
        value < 0.0 or not math.isfinite(value) for value in cfg.water_inj_rate_values
    ):
        raise ValueError("water-injection rate values must be finite and >= 0")
    if any(
        value <= 0.0 or not math.isfinite(value) for value in cfg.water_inj_bhp_values
    ):
        raise ValueError("water-injection BHP values must be finite and > 0")
    if cfg.water_inj_well_index < 0:
        raise ValueError("water_inj_well_index must be non-negative")
    if cfg.water_inj_prediction_index < 0:
        raise ValueError("water_inj_prediction_index must be non-negative")

    has_rate = bool(cfg.water_inj_rate_values)
    has_bhp = bool(cfg.water_inj_bhp_values)
    if not has_rate and not has_bhp:
        return
    if control == "rate" and not has_rate:
        raise ValueError("water_inj_control='rate' requires water_inj_rate_values")
    if control == "bhp" and not has_bhp:
        raise ValueError("water_inj_control='bhp' requires water_inj_bhp_values")
    if control == "rate_with_bhp_limit" and not has_rate:
        raise ValueError(
            "water_inj_control='rate_with_bhp_limit' requires water_inj_rate_values"
        )
    if has_rate and "water_inj_rate" not in cfg.tags:
        raise ValueError(
            "water_inj_rate_values set but tags['water_inj_rate'] is missing"
        )
    if has_bhp:
        if control == "bhp" and "water_inj_bhp" not in cfg.tags:
            raise ValueError(
                "water_inj_bhp_values set but tags['water_inj_bhp'] is missing"
            )
        if control != "bhp" and not (
            "water_inj_max_fbhp" in cfg.tags or "water_inj_bhp" in cfg.tags
        ):
            raise ValueError(
                "water_inj_bhp_values set but tags lack water_inj_max_fbhp / "
                "water_inj_bhp"
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


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    try:
        from scipy.stats import norm

        return np.asarray(norm.cdf(x), dtype=float)
    except ImportError:
        erf = np.vectorize(math.erf, otypes=[float])
        return 0.5 * (1.0 + erf(x / math.sqrt(2.0)))


def _correlate_unit(
    u: np.ndarray, shared: np.ndarray, correlation: float
) -> np.ndarray:
    """Couple a tank's unit draw to a shared factor, keeping it U(0,1).

    One-factor Gaussian copula: X = sqrt(rho) * Z + sqrt(1 - rho) * E, so
    corr(X_i, X_j) is exactly rho for two tanks on the same factor Z, and
    each X stays standard normal. The tank's own p_connected is therefore
    untouched — only the joint behaviour changes. rho = 1 collapses every
    tank in the group onto one draw (one barrier decides them all).
    """
    own = _norm_ppf(u)
    latent = math.sqrt(correlation) * shared + math.sqrt(1.0 - correlation) * own
    return _norm_cdf(latent)


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


def _sensitivity_factors(cfg: Config) -> list[tuple[str, tuple[float, ...]]]:
    """Deterministic operational knobs expanded across every volume sample."""
    factors: list[tuple[str, tuple[float, ...]]] = []
    if cfg.gas_lift_values:
        factors.append(("gas_lift_rate", cfg.gas_lift_values))
    if cfg.water_inj_rate_values:
        factors.append(("water_inj_rate", cfg.water_inj_rate_values))
    if cfg.water_inj_bhp_values:
        factors.append(("water_inj_bhp", cfg.water_inj_bhp_values))
    return factors


def _expand_sensitivity(samples: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Cartesian-expand volume samples by every operational sensitivity."""
    factors = _sensitivity_factors(cfg)
    if not factors:
        return samples
    out = samples.copy()
    out["base_realization"] = out["realization"]
    for name, values in factors:
        parts = []
        for value in values:
            block = out.copy()
            block[name] = float(value)
            parts.append(block)
        out = pd.concat(parts, ignore_index=True)
    out["realization"] = np.arange(len(out), dtype=int)
    return out


def build_sample_table(cfg: Config) -> pd.DataFrame:
    """Sample tank-volume inputs, then expand operational sensitivities."""
    validate_config(cfg)

    distributions: list[Distribution] = []
    model = cfg.volume_model
    connect_draws = 0
    if model.field_scale is not None:
        distributions.append(model.field_scale)
    for tank in cfg.tanks:
        residual = _tank_residual(cfg, tank)
        assert residual is not None
        distributions.append(residual)
        assert tank.connectivity is not None
        if 0.0 < tank.connectivity.p_connected < 1.0:
            connect_draws += 1
    distributions.extend(
        tank.aquifer_multiplier
        for tank in cfg.tanks
        if tank.aquifer_multiplier is not None
    )
    distributions.extend(
        tank.aquifer_volume for tank in cfg.tanks if tank.aquifer_volume is not None
    )
    groups = _correlated_connectivity_groups(cfg)
    base_dimensions = (
        sum(_needs_random_dimension(dist) for dist in distributions) + connect_draws
    )
    # Shared connectivity factors sit at the tail of the hypercube, so per-tank
    # column assignment - and any existing seed - is unchanged without groups.
    unit_samples = unit_hypercube(
        cfg.n_realizations, base_dimensions + len(groups), cfg
    )
    shared_factors = {
        name: _norm_ppf(unit_samples[:, base_dimensions + offset])
        for offset, name in enumerate(groups)
    }
    next_dimension = 0

    def draw(dist: Distribution) -> np.ndarray:
        nonlocal next_dimension
        if _needs_random_dimension(dist):
            u = unit_samples[:, next_dimension]
            next_dimension += 1
        else:
            u = None
        return sample_distribution(dist, u, cfg.n_realizations)

    def draw_unit() -> np.ndarray:
        nonlocal next_dimension
        u = unit_samples[:, next_dimension]
        next_dimension += 1
        return u

    data: dict[str, np.ndarray] = {
        "realization": np.arange(cfg.n_realizations, dtype=int)
    }
    stoiip_columns: list[str] = []

    if model.field_scale is not None:
        field_scale = draw(model.field_scale)
        data["field_scale"] = field_scale
    else:
        field_scale = np.ones(cfg.n_realizations, dtype=float)
    for tank in cfg.tanks:
        assert tank.official_stoiip is not None
        assert tank.connectivity is not None
        residual = _tank_residual(cfg, tank)
        assert residual is not None
        residual_samples = draw(residual)
        conn = tank.connectivity
        if 0.0 < conn.p_connected < 1.0:
            unit = draw_unit()
            if conn.group in shared_factors:
                unit = _correlate_unit(
                    unit,
                    shared_factors[conn.group],
                    model.connectivity_correlation,
                )
            connected = (unit < conn.p_connected).astype(float)
        else:
            connected = np.full(
                cfg.n_realizations, float(conn.p_connected >= 1.0), dtype=float
            )
        connect_frac = np.where(connected > 0.5, 1.0, float(conn.isolated_fraction))
        scale = (
            field_scale
            if tank.role.lower() == "base"
            else np.ones(cfg.n_realizations, dtype=float)
        )
        multipliers = residual_samples * scale
        if model.max_multiplier is not None:
            multipliers = np.minimum(multipliers, float(model.max_multiplier))
        data[f"official_{tank.key}"] = np.full(
            cfg.n_realizations, float(tank.official_stoiip), dtype=float
        )
        data[f"residual_{tank.key}"] = residual_samples
        data[f"connected_{tank.key}"] = connected
        data[f"connect_frac_{tank.key}"] = connect_frac
        column = f"stoiip_{tank.key}"
        data[column] = float(tank.official_stoiip) * connect_frac * multipliers
        stoiip_columns.append(column)

    data["stoiip_total"] = np.sum(
        np.column_stack([data[column] for column in stoiip_columns]), axis=1
    )
    base_cols = [f"stoiip_{tank.key}" for tank in _tanks_with_role(cfg, "base")]
    upside_cols = [f"stoiip_{tank.key}" for tank in _tanks_with_role(cfg, "upside")]
    in_model_cols = [f"stoiip_{tank.key}" for tank in _tanks_in_model(cfg)]
    if base_cols:
        data["stoiip_base"] = np.sum(
            np.column_stack([data[column] for column in base_cols]), axis=1
        )
    if upside_cols:
        data["stoiip_upside"] = np.sum(
            np.column_stack([data[column] for column in upside_cols]), axis=1
        )
    if in_model_cols:
        data["stoiip_in_model"] = np.sum(
            np.column_stack([data[column] for column in in_model_cols]), axis=1
        )

    for tank in cfg.tanks:
        if tank.aquifer_multiplier is not None:
            data[f"aq_mult_{tank.key}"] = draw(tank.aquifer_multiplier)
        if tank.aquifer_volume is not None:
            data[f"aquifer_volume_{tank.key}"] = draw(tank.aquifer_volume)

    samples = pd.DataFrame(data)
    return _expand_sensitivity(samples, cfg)


# -----------------------------------------------------------------------------
# 4. OPENSERVER SESSION
# -----------------------------------------------------------------------------


class SetServer(Protocol):
    def set(self, tag: str, value: float | str) -> None: ...


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

    def set(self, tag: str, value: float | str) -> None:
        self._check(self.os.DoSet(tag, value), f"DoSet {tag}")

    def get_raw(self, tag: str) -> Any:
        """Return the raw OpenServer value (numeric or list-keyword string)."""
        value = self.os.DoGet(tag)
        error = self.os.GetLastError("MBAL")
        if error:
            raise RuntimeError(
                f"OpenServer error reading {tag}: {self.os.GetErrorDescription(error)}"
            )
        return value

    def get(self, tag: str) -> float:
        return float(self.get_raw(tag))

    def try_get(self, tag: str) -> Any | None:
        """Return the raw value or None if the tag is not readable."""
        try:
            return self.get_raw(tag)
        except RuntimeError:
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
        well=cfg.gas_lift_well,
        i=cfg.gas_lift_well_index,
        p=cfg.gas_lift_prediction_index,
    )


def _format_inj_tag(cfg: Config, key: str) -> str:
    return cfg.tags[key].format(
        well=cfg.water_inj_well,
        i=cfg.water_inj_well_index,
        p=cfg.water_inj_prediction_index,
        tank="",
        u=cfg.unit_press,
    )


def input_tags_for_validation(cfg: Config) -> list[tuple[str, str]]:
    """Return (label, tag) pairs that should be readable/writable after open."""
    pairs: list[tuple[str, str]] = []
    for tank in _tanks_in_model(cfg):
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
    if cfg.water_inj_rate_values and "water_inj_rate" in cfg.tags:
        pairs.append(("water_inj_rate", _format_inj_tag(cfg, "water_inj_rate")))
    if cfg.water_inj_bhp_values:
        bhp_key = _water_inj_bhp_tag_key(cfg)
        if bhp_key in cfg.tags:
            pairs.append((bhp_key, _format_inj_tag(cfg, bhp_key)))
    return pairs


def _water_inj_bhp_tag_key(cfg: Config) -> str:
    if cfg.water_inj_control.lower() == "bhp":
        return "water_inj_bhp"
    if "water_inj_max_fbhp" in cfg.tags:
        return "water_inj_max_fbhp"
    return "water_inj_bhp"


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
    """Write each in-model tank's sampled inputs into MBAL."""
    for tank in _tanks_in_model(cfg):
        volume = max(float(row[f"stoiip_{tank.key}"]), cfg.min_tank_stoiip)
        srv.set(_stoiip_tag(cfg, tank), volume)
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

    _apply_water_inj(srv, row, cfg)


def _series_has(row: pd.Series, column: str) -> bool:
    return column in row.index and pd.notna(row[column])


def _apply_water_inj(srv: SetServer, row: pd.Series, cfg: Config) -> None:
    """Write water-injector rate / BHP. One well, both tanks (MBAL allocation)."""
    has_rate = _series_has(row, "water_inj_rate")
    has_bhp = _series_has(row, "water_inj_bhp")
    if not has_rate and not has_bhp:
        return

    if "pred_watinj" in cfg.tags:
        srv.set(_format_inj_tag(cfg, "pred_watinj"), "YES")

    control = cfg.water_inj_control.lower()
    if has_rate:
        rate = float(row["water_inj_rate"])
        srv.set(_format_inj_tag(cfg, "water_inj_rate"), rate)
        if control == "rate" and "water_inj_min_rate" in cfg.tags:
            srv.set(_format_inj_tag(cfg, "water_inj_min_rate"), rate)
        if "pred_max_inj_wat" in cfg.tags:
            srv.set(_format_inj_tag(cfg, "pred_max_inj_wat"), rate)

    if has_bhp:
        bhp = float(row["water_inj_bhp"])
        if control == "bhp":
            if "water_inj_perform" in cfg.tags:
                srv.set(_format_inj_tag(cfg, "water_inj_perform"), "CFBHP")
            srv.set(_format_inj_tag(cfg, "water_inj_bhp"), bhp)
        else:
            srv.set(_format_inj_tag(cfg, _water_inj_bhp_tag_key(cfg)), bhp)


def read_results(srv: OpenServer, cfg: Config, row: pd.Series) -> dict[str, float]:
    """Read the last prediction state for every configured tank."""
    output: dict[str, float] = {}
    for tank in _tanks_in_model(cfg):
        # Some IPM versions expose only the final state at index 0, so the
        # fallback is legitimate — but on a version that returns a time
        # series, index 0 is the FIRST step and every realization would
        # report ~0 cumulative oil as a successful run. Say so, loudly.
        template = cfg.tags.get("res_nsteps")
        nsteps_tag = template.format(i=tank.index) if template else None
        last = 0
        if nsteps_tag is None:
            _warn_once(
                "res_nsteps/missing",
                "tags['res_nsteps'] is not configured - reading prediction results "
                "at index 0. If this IPM version returns a time series, that is the "
                "first step and cumulative oil will read as ~0 for every "
                "realization.",
            )
        else:
            try:
                last = max(int(srv.get(nsteps_tag)) - 1, 0)
            except (RuntimeError, TypeError, ValueError):
                _warn_once(
                    f"res_nsteps/{nsteps_tag}",
                    "cannot read step count %s - falling back to prediction results "
                    "index 0. If this IPM version returns a time series, that is the "
                    "first step and cumulative oil will read as ~0 for every "
                    "realization. Check res_nsteps against MBAL's OpenServer "
                    "browser.",
                    nsteps_tag,
                )

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

        cumulative_inj = float("nan")
        if "res_cumwatinj" in cfg.tags:
            try:
                cumulative_inj = srv.get(
                    cfg.tags["res_cumwatinj"].format(
                        i=tank.index, k=last, u=cfg.unit_cum
                    )
                )
            except (RuntimeError, KeyError):
                cumulative_inj = float("nan")

        stoiip = float(row[f"stoiip_{tank.key}"])
        output[f"np_{tank.key}"] = cumulative_oil
        output[f"pres_{tank.key}"] = pressure
        output[f"wp_{tank.key}"] = cumulative_water
        output[f"wi_{tank.key}"] = cumulative_inj
        if stoiip == 0.0 or not math.isfinite(stoiip):
            output[f"rf_{tank.key}"] = float("nan")
        else:
            output[f"rf_{tank.key}"] = cumulative_oil / stoiip

    modelled = _tanks_in_model(cfg)
    output["np_total"] = sum(output[f"np_{tank.key}"] for tank in modelled)
    inj_values = [output[f"wi_{tank.key}"] for tank in modelled]
    if inj_values and all(math.isfinite(value) for value in inj_values):
        output["wi_total"] = sum(inj_values)
    else:
        output["wi_total"] = float("nan")
    if "stoiip_in_model" in row.index:
        stoiip_for_rf = float(row["stoiip_in_model"])
    else:
        stoiip_for_rf = sum(float(row[f"stoiip_{tank.key}"]) for tank in modelled)
    if stoiip_for_rf == 0.0 or not math.isfinite(stoiip_for_rf):
        output["rf_total"] = float("nan")
    else:
        output["rf_total"] = output["np_total"] / stoiip_for_rf
    return output


def new_result_record(row: pd.Series, cfg: Config) -> dict[str, object]:
    """Create a stable CSV row schema before the OpenServer call can fail."""
    record: dict[str, object] = row.to_dict()
    record["realization"] = int(row["realization"])
    for tank in cfg.tanks:
        for prefix in ("np", "pres", "wp", "wi", "rf"):
            record[f"{prefix}_{tank.key}"] = float("nan")
    record["np_total"] = float("nan")
    record["wi_total"] = float("nan")
    record["rf_total"] = float("nan")
    record["status"] = "not run"
    record["runtime_s"] = float("nan")
    return record


def _input_columns(cfg: Config) -> list[str]:
    columns = [f"stoiip_{tank.key}" for tank in cfg.tanks]
    columns.append("stoiip_total")
    if cfg.volume_model.field_scale is not None:
        columns.append("field_scale")
    columns.extend(f"official_{tank.key}" for tank in cfg.tanks)
    columns.extend(f"residual_{tank.key}" for tank in cfg.tanks)
    columns.extend(f"connected_{tank.key}" for tank in cfg.tanks)
    columns.extend(f"connect_frac_{tank.key}" for tank in cfg.tanks)
    if _tanks_with_role(cfg, "base"):
        columns.append("stoiip_base")
    if _tanks_with_role(cfg, "upside"):
        columns.append("stoiip_upside")
    if _tanks_in_model(cfg):
        columns.append("stoiip_in_model")
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
    factors = _sensitivity_factors(cfg)
    if factors:
        columns.append("base_realization")
        columns.extend(name for name, _values in factors)
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
    if "stoiip_base" in df.columns:
        columns.append(("stoiip_base", f"Base connected STOIIP [{cfg.unit_stoiip}]"))
    if "stoiip_upside" in df.columns:
        columns.append(("stoiip_upside", f"Upside-sand STOIIP [{cfg.unit_stoiip}]"))
    columns.append(("stoiip_total", f"Field STOIIP [{cfg.unit_stoiip}]"))

    if not include_prediction_results:
        return columns

    for prefix, label, unit in (
        ("np", "cumulative oil", cfg.unit_cum),
        ("pres", "pressure", cfg.unit_press),
        ("wp", "cumulative water", cfg.unit_cum),
        ("wi", "cumulative water injected", cfg.unit_cum),
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
    _summarize_operational_sweep(
        df,
        cfg,
        group_columns=("gas_lift_rate",),
        filename="gas_lift_sensitivity",
        x_column="gas_lift_rate",
        x_label="Gas lift rate [current MBAL model units]",
        title="Gas-lift sensitivity",
    )


def _summarize_water_inj(df: pd.DataFrame, cfg: Config) -> None:
    """Write and plot field-oil sensitivity versus injector rate and/or BHP."""
    group_columns = tuple(
        name
        for name in ("water_inj_rate", "water_inj_bhp")
        if name in df.columns
    )
    if not group_columns:
        return
    x_column = "water_inj_rate" if "water_inj_rate" in group_columns else "water_inj_bhp"
    if x_column == "water_inj_rate":
        x_label = "Water injection rate [current MBAL model units]"
    else:
        x_label = "Injector BHP [current MBAL model units]"
    _summarize_operational_sweep(
        df,
        cfg,
        group_columns=group_columns,
        filename="water_inj_sensitivity",
        x_column=x_column,
        x_label=x_label,
        title="Water-injection sensitivity",
        series_column="water_inj_bhp" if len(group_columns) == 2 else None,
    )


def _summarize_operational_sweep(
    df: pd.DataFrame,
    cfg: Config,
    *,
    group_columns: tuple[str, ...],
    filename: str,
    x_column: str,
    x_label: str,
    title: str,
    series_column: str | None = None,
) -> None:
    if x_column not in df.columns or "np_total" not in df.columns:
        return
    missing = [column for column in group_columns if column not in df.columns]
    if missing:
        return
    successful = df[df["status"] == "ok"] if "status" in df.columns else df
    if successful.empty:
        return

    rows = []
    for keys, group in successful.groupby(list(group_columns), sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        stats = percentiles(
            group["np_total"].to_numpy(dtype=float), extra=cfg.extra_percentiles
        )
        record = {name: value for name, value in zip(group_columns, keys)}
        record.update(stats)
        record["n"] = len(group)
        rows.append(record)
    sensitivity = pd.DataFrame(rows)
    path = os.path.join(cfg.out_dir, f"{filename}.csv")
    sensitivity.to_csv(path, index=False)
    LOG.info("wrote %s", path)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    figure, axes = plt.subplots(figsize=(7, 4))
    if series_column and series_column in sensitivity.columns:
        for series_value, series in sensitivity.groupby(series_column, sort=True):
            ordered = series.sort_values(x_column)
            axes.plot(
                ordered[x_column].to_numpy(dtype=float),
                ordered["P50"].to_numpy(dtype=float),
                marker="o",
                label=f"P50 @ {series_column}={series_value:g}",
            )
    else:
        ordered = sensitivity.sort_values(x_column)
        x = ordered[x_column].to_numpy(dtype=float)
        for column in ("P90", "P50", "P10", "mean"):
            if column in ordered.columns:
                axes.plot(x, ordered[column], marker="o", label=column)
    axes.set_xlabel(x_label)
    axes.set_ylabel(f"Field cumulative oil [{cfg.unit_cum}]")
    axes.set_title(title)
    axes.grid(alpha=0.3)
    axes.legend()
    figure.tight_layout()
    plot_path = os.path.join(cfg.out_dir, f"{filename}.png")
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
    axes[0].set_title("Field STOIIP (connected volume, incl. optional upside)")
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

    operational_sweep = bool(_sensitivity_factors(cfg))
    # Treat leftover sweep columns from a prior CSV as sweep mode.
    if "base_realization" in df.columns and any(
        column in df.columns
        for column in ("gas_lift_rate", "water_inj_rate", "water_inj_bhp")
    ):
        operational_sweep = True

    if operational_sweep:
        # Volume inputs are repeated once per operational setting. Summarize
        # each volume realization once; prediction metrics go to the
        # per-setting sensitivity tables.
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
        include_prediction_results=not operational_sweep,
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
        "volume_model": cfg.volume_model.kind,
        "connectivity_correlation": cfg.volume_model.connectivity_correlation,
        "gas_lift_values": list(cfg.gas_lift_values),
        "water_inj_rate_values": list(cfg.water_inj_rate_values),
        "water_inj_bhp_values": list(cfg.water_inj_bhp_values),
        "water_inj_control": cfg.water_inj_control,
    }
    meta_path = os.path.join(cfg.out_dir, "run_metadata.csv")
    pd.DataFrame([meta]).to_csv(meta_path, index=False)
    LOG.info("wrote %s", meta_path)

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        LOG.warning("matplotlib not installed - skipping plots")
        if operational_sweep:
            _summarize_gas_lift(df, cfg)
            _summarize_water_inj(df, cfg)
            _summarize_decision(df, cfg)
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
    if not operational_sweep:
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
    _summarize_water_inj(df, cfg)
    _summarize_decision(df, cfg)
    return summary


def _volume_source(samples: pd.DataFrame) -> pd.DataFrame:
    if "base_realization" in samples.columns:
        return samples.drop_duplicates("base_realization", keep="first")
    return samples


def _print_volume_diagnostics(samples: pd.DataFrame, cfg: Config) -> None:
    """Print official vs sampled STOIIP so a high-side prior is visible."""
    source = _volume_source(samples)
    print(
        "\nVolume model: connected_volume "
        f"(connectivity_correlation={cfg.volume_model.connectivity_correlation})"
    )
    print("Official numbers are working mapped cases, not the mean.")
    rows = []
    for tank in cfg.tanks:
        column = f"stoiip_{tank.key}"
        if column not in source.columns or tank.official_stoiip is None:
            continue
        stats = percentiles(source[column].to_numpy(), extra=cfg.extra_percentiles)
        official = float(tank.official_stoiip)
        p_conn = float("nan")
        if f"connected_{tank.key}" in source.columns:
            p_conn = float(source[f"connected_{tank.key}"].mean())
        rows.append(
            {
                "tank": tank.key,
                "role": tank.role,
                "official": official,
                "P_connect": p_conn,
                "P90": stats["P90"],
                "P50": stats["P50"],
                "P10": stats["P10"],
                "mean": stats["mean"],
                "mean/official": stats["mean"] / official if official else float("nan"),
            }
        )
    for label, column, official in (
        (
            "base",
            "stoiip_base",
            sum(
                float(tank.official_stoiip)
                for tank in _tanks_with_role(cfg, "base")
                if tank.official_stoiip is not None
            ),
        ),
        (
            "upside",
            "stoiip_upside",
            sum(
                float(tank.official_stoiip)
                for tank in _tanks_with_role(cfg, "upside")
                if tank.official_stoiip is not None
            ),
        ),
        (
            "field",
            "stoiip_total",
            sum(
                float(tank.official_stoiip)
                for tank in cfg.tanks
                if tank.official_stoiip is not None
            ),
        ),
    ):
        if column not in source.columns:
            continue
        stats = percentiles(source[column].to_numpy(), extra=cfg.extra_percentiles)
        rows.append(
            {
                "tank": label,
                "role": "",
                "official": official,
                "P_connect": float("nan"),
                "P90": stats["P90"],
                "P50": stats["P50"],
                "P10": stats["P10"],
                "mean": stats["mean"],
                "mean/official": stats["mean"] / official if official else float("nan"),
            }
        )
    if rows:
        table = pd.DataFrame(rows)
        print(table.round(3).to_string(index=False))
        base = next((row for row in rows if row["tank"] == "base"), None)
        if base and base["mean"] > base["official"] * 1.01:
            print(
                "WARNING: sampled base mean exceeds official base total — "
                "the prior is optimistic on expected connected STOIIP."
            )


def _summarize_decision(df: pd.DataFrame, cfg: Config) -> None:
    """Write a one-page decision table: base vs upside vs total STOIIP."""
    source = _volume_source(df)
    rows = []
    scopes = [
        ("base (sidetrack without deeper sand)", "stoiip_base"),
        ("upside sand only", "stoiip_upside"),
        ("total including upside sand", "stoiip_total"),
    ]
    for label, column in scopes:
        if column not in source.columns:
            continue
        stats = percentiles(source[column].to_numpy(), extra=cfg.extra_percentiles)
        rows.append({"scope": label, **{k: round(v, 3) for k, v in stats.items()}})
    for tank in cfg.tanks:
        conn_col = f"connected_{tank.key}"
        if conn_col not in source.columns:
            continue
        rows.append(
            {
                "scope": f"P(connected {tank.key})",
                "mean": round(float(source[conn_col].mean()), 3),
            }
        )
    # The joint number is the one that shows whether the shared barrier was
    # modelled as shared: independent draws make this far too small.
    base_connected = [
        f"connected_{tank.key}"
        for tank in _tanks_with_role(cfg, "base")
        if f"connected_{tank.key}" in source.columns
    ]
    if len(base_connected) > 1:
        isolated = (source[base_connected].to_numpy(dtype=float) < 0.5).all(axis=1)
        rows.append(
            {
                "scope": "P(all base tanks isolated)",
                "mean": round(float(isolated.mean()), 3),
            }
        )
    if not rows:
        return
    table = pd.DataFrame(rows)
    path = os.path.join(cfg.out_dir, "decision_volume_summary.csv")
    table.to_csv(path, index=False)
    LOG.info("wrote %s", path)
    print("\nDecision volume table (working official numbers):")
    print(table.to_string(index=False))


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


def build_arg_parser(description: str) -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--gas-lift-values",
        help=(
            "comma-separated gas-lift sensitivity values in the current MBAL "
            "model units, e.g. 0,0.5,1.0,1.5"
        ),
    )
    parser.add_argument(
        "--water-inj-rate-values",
        help=(
            "comma-separated water-injection rate sensitivity values in the "
            "current MBAL model units, e.g. 0,300,600"
        ),
    )
    parser.add_argument(
        "--water-inj-bhp-values",
        help=(
            "comma-separated injector BHP / BHP-limit sensitivity values in "
            "the current MBAL model units, e.g. 250,300"
        ),
    )
    parser.add_argument(
        "--water-inj-control",
        choices=("rate", "bhp", "rate_with_bhp_limit"),
        help=(
            "how the injector is controlled: fixed rate, fixed BHP "
            "(PERFORMTYPE=CFBHP), or rate with a BHP limit"
        ),
    )
    parser.add_argument(
        "--connectivity-correlation",
        type=float,
        metavar="RHO",
        help=(
            "0-1 coupling between tanks sharing a connectivity group. Run 0 "
            "and 1 as bounding cases: 0 = the sands are isolated by different "
            "barriers, 1 = one barrier decides them together"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sample and report inputs only; never open MBAL",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help=(
            "validate YAML and reject unresolved example values; do not create "
            "outputs or open MBAL"
        ),
    )
    parser.add_argument(
        "--check-openserver",
        action="store_true",
        help=(
            "Windows smoke check: dispatch COM, open the model and read configured "
            "input tags; do not write inputs or run prediction"
        ),
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


def apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
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
    if getattr(args, "gas_lift_values", None):
        try:
            updates["gas_lift_values"] = _parse_float_tuple(
                args.gas_lift_values, "--gas-lift-values"
            )
        except ValueError as error:
            raise SystemExit(f"invalid --gas-lift-values: {error}") from error
    if getattr(args, "water_inj_rate_values", None):
        try:
            updates["water_inj_rate_values"] = _parse_float_tuple(
                args.water_inj_rate_values, "--water-inj-rate-values"
            )
        except ValueError as error:
            raise SystemExit(f"invalid --water-inj-rate-values: {error}") from error
    if getattr(args, "water_inj_bhp_values", None):
        try:
            updates["water_inj_bhp_values"] = _parse_float_tuple(
                args.water_inj_bhp_values, "--water-inj-bhp-values"
            )
        except ValueError as error:
            raise SystemExit(f"invalid --water-inj-bhp-values: {error}") from error
    if getattr(args, "water_inj_control", None):
        updates["water_inj_control"] = args.water_inj_control
    if getattr(args, "connectivity_correlation", None) is not None:
        updates["volume_model"] = replace(
            cfg.volume_model,
            connectivity_correlation=args.connectivity_correlation,
        )
    return replace(cfg, **updates) if updates else cfg


def write_example_config(path: str) -> None:
    try:
        import yaml
    except ImportError as error:
        raise SystemExit(
            "PyYAML is required to write example config; pip install pyyaml"
        ) from error

    cfg = default_config(
        water_inj_rate_values=(0.0, 300.0, 600.0),
        water_inj_bhp_values=(250.0, 300.0),
    )
    data = config_to_dict(cfg)
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
    print(f"wrote example config to {path}")


def main(
    argv: list[str] | None = None,
    *,
    description: str | None = None,
) -> int:
    """CLI entry: three tanks, optional gas-lift and water-injection sweeps."""
    desc = description or (
        "Probabilistic MBAL via OpenServer "
        "(three-tank connected volume; optional gas-lift and water injection)"
    )
    parser = build_arg_parser(desc)
    args = parser.parse_args(argv)

    if args.write_example_config:
        write_example_config(args.write_example_config)
        return 0

    cfg = default_config()
    if args.config:
        cfg = load_config_yaml(args.config, base=cfg)
    cfg = apply_cli_overrides(cfg, args)
    validate_config(cfg)

    if args.validate_config or args.check_openserver:
        validate_licensed_run_config(cfg, check_model_file=sys.platform == "win32")

    if args.validate_config and not args.check_openserver:
        print(
            "Configuration is structurally valid and has no unresolved "
            "example values. No MBAL/OpenServer session was opened; verify "
            "version-specific tags with --check-openserver on the licensed "
            "Windows host."
        )
        return 0

    if args.check_openserver:
        if sys.platform != "win32":
            print(
                "OpenServer smoke check is Windows-only. No COM dispatch was attempted.",
                file=sys.stderr,
            )
            return 1
        setup_logging(cfg.log_level, log_file=None)
        srv: OpenServer | None = None
        try:
            # A smoke check must probe tags even if a full campaign later opts out.
            smoke_cfg = replace(cfg, validate_tags=True)
            srv = _open_model(smoke_cfg)
        except Exception as error:
            LOG.exception("OpenServer smoke check failed")
            print(f"OpenServer smoke check failed: {error}", file=sys.stderr)
            return 1
        finally:
            if srv is not None:
                srv.shutdown(cfg.tags["cmd_close"])
        print(
            "OpenServer smoke check passed: COM dispatch, model open, and configured "
            "input-tag reads succeeded. No input was written and no prediction was "
            "run; use a one-realization licensed run to verify the command and results."
        )
        return 0

    if sys.platform == "win32" and not args.dry_run and not args.summarize_only:
        # Reject placeholders and a missing model before sampling or creating output.
        validate_licensed_run_config(cfg, check_model_file=True)

    log_path = os.path.join(cfg.out_dir, "mbal_run.log")
    setup_logging(cfg.log_level, log_file=None)  # file after out_dir known
    # Always log to stdout; also to out_dir when not dry-run only is fine always
    os.makedirs(cfg.out_dir, exist_ok=True)
    setup_logging(cfg.log_level, log_file=log_path)

    if args.summarize_only:
        path = os.path.join(cfg.out_dir, cfg.out_csv)
        if not os.path.exists(path):
            LOG.error("no results CSV at %s", path)
            print(
                f"No results CSV at {path}.\n"
                "Run without --summarize-only first, or point --out-dir at the "
                f"directory holding {cfg.out_csv}.",
                file=sys.stderr,
            )
            return 1
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
            "(base tanks share field_scale and their connectivity group; "
            "the upside sand is independent):"
        )
        rank_correlation = samples[stoiip_columns].rank().corr()
        print(rank_correlation.round(3).to_string())
    _print_volume_diagnostics(samples, cfg)

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


TAGS = dict(DEFAULT_NAME_TAGS)
CFG = default_config()

__all__ = [
    "CFG",
    "DEFAULT_INDEX_TAGS",
    "DEFAULT_NAME_TAGS",
    "TAGS",
    "Config",
    "Connectivity",
    "Distribution",
    "OpenServer",
    "SetServer",
    "TankConfig",
    "VolumeModel",
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
    "validate_licensed_run_config",
    "validate_openserver_tags",
]
