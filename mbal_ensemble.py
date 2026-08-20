"""Sample tank volumes for repeated runs of the verified simple MBAL model.

The deterministic OpenServer coupling stays in :mod:`mbal_simple`. This module
adds only the ensemble layer: independent per-tank volume samples, with the
configured ``stoiip`` interpreted as the arithmetic mean and optional O&G
P90/P10 anchors.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

import mbal_simple as simple

Z90 = NormalDist().inv_cdf(0.9)
_NORMAL = NormalDist()


@dataclass(frozen=True)
class VolumePrior:
    """A positive split lognormal calibrated to one tank's P90, mean and P10."""

    median: float
    sigma_low: float
    sigma_high: float
    # Other medians that reproduce the same three statistics. A non-empty
    # tuple means those three numbers do not pin down one distribution.
    alternatives: tuple[float, ...] = ()


def volume_column(tank: simple.Tank) -> str:
    """Stable, readable sample/result column for one tank volume."""
    return f"{tank.name}.OOIP"


def _normal_ppf(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=float), 1e-12, 1.0 - 1e-12)
    return np.fromiter(
        (_NORMAL.inv_cdf(float(value)) for value in clipped),
        dtype=float,
        count=clipped.size,
    )


def _unit_hypercube(
    n: int, dimensions: int, *, seed: int, sampling: str
) -> np.ndarray:
    if n <= 0:
        raise ValueError("n_realizations must be greater than zero")
    if sampling not in {"lhs", "mc"}:
        raise ValueError("sampling must be lhs or mc")
    if dimensions == 0:
        return np.empty((n, 0), dtype=float)

    rng = np.random.default_rng(seed)
    if sampling == "mc":
        return rng.random((n, dimensions))

    values = np.empty((n, dimensions), dtype=float)
    for column in range(dimensions):
        strata = rng.permutation(n)
        values[:, column] = (strata + rng.random(n)) / n
    return values


def _split_lognormal_mean(
    median: float, p90: float, p10: float
) -> tuple[float, float, float]:
    """Return theoretical mean and side sigmas for a proposed median."""
    sigma_low = math.log(median / p90) / Z90
    sigma_high = math.log(p10 / median) / Z90
    mean = median * (
        math.exp(0.5 * sigma_low**2) * _NORMAL.cdf(-sigma_low)
        + math.exp(0.5 * sigma_high**2) * _NORMAL.cdf(sigma_high)
    )
    return mean, sigma_low, sigma_high


def _mean_anchored_split_lognormal(
    p90: float, mean: float, p10: float
) -> VolumePrior:
    """Calibrate a positive split lognormal to P90, arithmetic mean and P10.

    The median is the third fitted parameter. Each side is lognormal in the
    standard-normal quantile, continuous at that median. Some extreme triples
    are not representable by this family; those fail before any MBAL call.

    For wide P10/P90 ratios the mean is not monotone in the median, so more
    than one distribution can reproduce the same three statistics. The rival
    medians are returned in ``alternatives`` rather than discarded silently.
    """
    if not all(math.isfinite(value) for value in (p90, mean, p10)) or not (
        0.0 < p90 < mean < p10
    ):
        raise ValueError("require 0 < P90 < mean < P10")

    lower = math.nextafter(p90, p10)
    upper = math.nextafter(p10, p90)
    medians = np.geomspace(lower, upper, 1025)
    residuals = np.array(
        [_split_lognormal_mean(float(value), p90, p10)[0] - mean for value in medians]
    )

    candidates: list[tuple[float, float, float]] = []
    for index in range(len(medians) - 1):
        left = float(medians[index])
        right = float(medians[index + 1])
        f_left = float(residuals[index])
        f_right = float(residuals[index + 1])
        if abs(f_left) <= 1e-12 * mean:
            fitted_mean, sigma_low, sigma_high = _split_lognormal_mean(
                left, p90, p10
            )
            candidates.append((left, sigma_low, sigma_high))
            continue
        if f_left * f_right > 0.0:
            continue
        for _ in range(80):
            middle = 0.5 * (left + right)
            f_middle = _split_lognormal_mean(middle, p90, p10)[0] - mean
            if f_left * f_middle <= 0.0:
                right = middle
                f_right = f_middle
            else:
                left = middle
                f_left = f_middle
        median = 0.5 * (left + right)
        fitted_mean, sigma_low, sigma_high = _split_lognormal_mean(
            median, p90, p10
        )
        if math.isclose(fitted_mean, mean, rel_tol=1e-10, abs_tol=1e-12):
            candidates.append((median, sigma_low, sigma_high))

    if not candidates:
        raise ValueError(
            "cannot match its mean, P90 and P10 with the positive split-lognormal "
            "volume prior; check the three GeoX statistics"
        )

    unique: list[tuple[float, float, float]] = []
    for candidate in sorted(candidates):
        if not unique or not math.isclose(
            candidate[0], unique[-1][0], rel_tol=1e-9
        ):
            unique.append(candidate)
    candidates = unique

    lower_spread = mean - p90
    upper_spread = p10 - mean
    if upper_spread > lower_spread:
        consistent = [candidate for candidate in candidates if candidate[0] <= mean]
    elif lower_spread > upper_spread:
        consistent = [candidate for candidate in candidates if candidate[0] >= mean]
    else:
        consistent = candidates
    pool = consistent or candidates
    median, sigma_low, sigma_high = min(
        pool, key=lambda candidate: abs(candidate[0] - mean)
    )
    return VolumePrior(
        median=median,
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        alternatives=tuple(
            candidate[0] for candidate in candidates if candidate[0] != median
        ),
    )


def fit_tank_prior(tank: simple.Tank) -> VolumePrior | None:
    """The calibrated volume prior for one tank, or None when it is fixed."""
    if tank.p90_stoiip is None and tank.p10_stoiip is None:
        return None
    if tank.p90_stoiip is None or tank.p10_stoiip is None:
        raise ValueError(
            f"tank {tank.name}: p90_stoiip and p10_stoiip must be given together"
        )
    try:
        return _mean_anchored_split_lognormal(
            float(tank.p90_stoiip),
            float(tank.inputs["stoiip"]),
            float(tank.p10_stoiip),
        )
    except ValueError as error:
        raise ValueError(f"tank {tank.name}: {error}") from error


def _sample_tank(
    tank: simple.Tank,
    prior: VolumePrior | None,
    unit_values: np.ndarray | None,
    n: int,
) -> np.ndarray:
    if prior is None:
        return np.full(n, float(tank.inputs["stoiip"]), dtype=float)
    if unit_values is None:
        raise ValueError(f"tank {tank.name}: uncertain volume needs a sample dimension")

    normal = _normal_ppf(unit_values)
    sigma = np.where(normal < 0.0, prior.sigma_low, prior.sigma_high)
    return prior.median * np.exp(sigma * normal)


def build_sample_table(cfg: simple.Config) -> pd.DataFrame:
    """Build reproducible, independent tank-volume realizations and field sums."""
    priors = {tank.name: fit_tank_prior(tank) for tank in cfg.tanks}
    for tank in cfg.tanks:
        prior = priors[tank.name]
        if prior is None or not prior.alternatives:
            continue
        rivals = ", ".join(f"{value:.6g}" for value in prior.alternatives)
        print(
            f"warning: tank {tank.name}: its P90, mean and P10 are matched by "
            f"more than one prior; sampling the one with median "
            f"{prior.median:.6g}, not median {rivals}. The three statistics do "
            "not pin down the spread; check the fitted columns in "
            "ensemble_summary.csv.",
            file=sys.stderr,
        )
    unit_values = _unit_hypercube(
        cfg.n_realizations,
        sum(prior is not None for prior in priors.values()),
        seed=cfg.seed,
        sampling=cfg.sampling,
    )
    data: dict[str, np.ndarray] = {
        "realization": np.arange(cfg.n_realizations, dtype=int)
    }
    columns: list[str] = []
    dimension = 0
    for tank in cfg.tanks:
        prior = priors[tank.name]
        unit = None
        if prior is not None:
            unit = unit_values[:, dimension]
            dimension += 1
        column = volume_column(tank)
        data[column] = _sample_tank(tank, prior, unit, cfg.n_realizations)
        columns.append(column)
    data["stoiip_total"] = np.sum(
        np.column_stack([data[column] for column in columns]), axis=1
    )
    return pd.DataFrame(data)


def _config_for_sample(cfg: simple.Config, row: pd.Series) -> simple.Config:
    tanks = tuple(
        replace(
            tank,
            inputs={**tank.inputs, "stoiip": float(row[volume_column(tank)])},
        )
        for tank in cfg.tanks
    )
    return replace(cfg, tanks=tanks)


def _load_resume_records(
    path: Path, samples: pd.DataFrame
) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    previous = pd.read_csv(path)
    required = set(samples.columns)
    missing = required.difference(previous.columns)
    if missing:
        raise RuntimeError(
            f"cannot resume {path}: missing sample columns {', '.join(sorted(missing))}"
        )
    previous = previous.drop_duplicates("realization", keep="last")
    expected = samples.set_index("realization")
    records: dict[int, dict[str, Any]] = {}
    for _, old_row in previous.iterrows():
        realization = int(old_row["realization"])
        if realization not in expected.index:
            raise RuntimeError(
                f"cannot resume {path}: realization {realization} is not in the "
                "current sample table"
            )
        for column in samples.columns:
            if column == "realization":
                continue
            old = float(old_row[column])
            new = float(expected.at[realization, column])
            if not np.isclose(old, new, rtol=1e-10, atol=1e-12):
                raise RuntimeError(
                    f"cannot resume {path}: realization {realization} has a "
                    f"different {column}; n, seed or volume statistics changed"
                )
        records[realization] = old_row.to_dict()
    return records


def _write_records(path: Path, records: dict[int, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([records[key] for key in sorted(records)])
    temporary = path.with_name(f".{path.name}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _load_profile_records(path: Path) -> dict[tuple[int, int], dict[str, Any]]:
    if not path.exists():
        return {}
    profiles = pd.read_csv(path)
    if not {"realization", "step"}.issubset(profiles.columns):
        raise RuntimeError(
            f"cannot resume {path}: profile file lacks realization and step"
        )
    return {
        (int(row["realization"]), int(row["step"])): row.to_dict()
        for _, row in profiles.iterrows()
    }


def _write_profile_records(
    path: Path, records: dict[tuple[int, int], dict[str, Any]]
) -> None:
    if not records:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([records[key] for key in sorted(records)])
    temporary = path.with_name(f".{path.name}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _valid_required_result(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, (int, float, np.number)):
        return math.isfinite(float(value))
    return True


def _record_is_complete(record: dict[str, Any], cfg: simple.Config) -> bool:
    return record.get("status") == "ok" and all(
        name in record and _valid_required_result(record[name])
        for name in cfg.results.read
    )


def run_ensemble(
    samples: pd.DataFrame,
    cfg: simple.Config,
    srv: Any,
) -> pd.DataFrame:
    """Run every pending sample through the verified simple MBAL coupling.

    Successful existing rows are skipped. Failed rows are retried, and resume
    stops before opening MBAL if any stored volume differs from the regenerated
    deterministic sample table.
    """
    results_path = Path(cfg.out_dir) / "ensemble_results.csv"
    profiles_path = Path(cfg.out_dir) / "ensemble_profiles.csv"
    records = _load_resume_records(results_path, samples)
    profile_records = (
        _load_profile_records(profiles_path) if cfg.results.profile else {}
    )
    pending = [
        int(value)
        for value in samples["realization"]
        if not _record_is_complete(records.get(int(value), {}), cfg)
    ]
    if not pending:
        return pd.DataFrame([records[key] for key in sorted(records)])

    simple.open_model(cfg, srv)
    pending_set = set(pending)
    for position, (_, row) in enumerate(samples.iterrows()):
        realization = int(row["realization"])
        if realization not in pending_set:
            continue
        record: dict[str, Any] = row.to_dict()
        record["realization"] = realization
        record.update(
            {
                name: float("nan") for name in cfg.results.read
            }
        )
        record.update(
            {
                "status": "failed",
                "error": "",
                "runtime_s": float("nan"),
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        started = time.time()
        profile_rows: list[dict[str, Any]] = []
        try:
            realization_cfg = _config_for_sample(cfg, row)
            mismatched = simple.write_inputs(realization_cfg, srv)
            if mismatched:
                raise RuntimeError(
                    "MBAL did not accept these written values: " + "; ".join(mismatched)
                )
            prediction_results = simple.run_prediction(realization_cfg, srv)
            invalid = [
                name
                for name, value in prediction_results.items()
                if not _valid_required_result(value)
            ]
            if invalid:
                raise RuntimeError(
                    "prediction returned missing or non-finite required result(s): "
                    + ", ".join(invalid)
                )
            record.update(prediction_results)
            if cfg.results.profile:
                profile_rows = simple.read_profile(realization_cfg, srv)
            record["status"] = "ok"
        except Exception as error:  # noqa: BLE001 - preserve a failed campaign row
            record["error"] = str(error)
        record["runtime_s"] = round(time.time() - started, 3)
        if cfg.results.profile:
            profile_records = {
                key: value
                for key, value in profile_records.items()
                if key[0] != realization
            }
            for profile_row in profile_rows:
                step = int(profile_row["step"])
                profile_records[(realization, step)] = {
                    "realization": realization,
                    **profile_row,
                }
            _write_profile_records(profiles_path, profile_records)
        records[realization] = record
        _write_records(results_path, records)

        if record["status"] != "ok" and any(
            candidate in pending_set
            for candidate in samples["realization"].iloc[position + 1 :]
        ):
            simple.open_model(cfg, srv)

    # Do not leave the interactive MBAL session showing an arbitrary final
    # realization. Reload the saved model after all result rows are durable.
    simple.open_model(cfg, srv)
    return pd.DataFrame([records[key] for key in sorted(records)])


def _statistics(values: pd.Series) -> dict[str, float | int]:
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    numeric = numeric[np.isfinite(numeric)]
    if not numeric.size:
        return {
            "sampled_P90": float("nan"),
            "sampled_P50": float("nan"),
            "sampled_P10": float("nan"),
            "sampled_mean": float("nan"),
            "sampled_std": float("nan"),
            "n": 0,
        }
    return {
        "sampled_P90": float(np.percentile(numeric, 10)),
        "sampled_P50": float(np.percentile(numeric, 50)),
        "sampled_P10": float(np.percentile(numeric, 90)),
        "sampled_mean": float(numeric.mean()),
        "sampled_std": float(numeric.std(ddof=0)),
        "n": int(numeric.size),
    }


def _prior_columns(prior: VolumePrior | None) -> dict[str, float | int]:
    """The fitted prior, so a campaign records which distribution it sampled."""
    if prior is None:
        return {
            "fitted_median": float("nan"),
            "fitted_sigma_low": float("nan"),
            "fitted_sigma_high": float("nan"),
            "fitted_rivals": 0,
        }
    return {
        "fitted_median": prior.median,
        "fitted_sigma_low": prior.sigma_low,
        "fitted_sigma_high": prior.sigma_high,
        "fitted_rivals": len(prior.alternatives),
    }


def summarize(data: pd.DataFrame, cfg: simple.Config) -> pd.DataFrame:
    """Write input-volume and successful prediction percentiles."""
    rows: list[dict[str, Any]] = []
    for tank in cfg.tanks:
        column = volume_column(tank)
        mean = float(tank.inputs["stoiip"])
        p90 = mean if tank.p90_stoiip is None else float(tank.p90_stoiip)
        p10 = mean if tank.p10_stoiip is None else float(tank.p10_stoiip)
        rows.append(
            {
                "variable": column,
                "target_P90": p90,
                "target_mean": mean,
                "target_P10": p10,
                **_statistics(data[column]),
                **_prior_columns(fit_tank_prior(tank)),
            }
        )

    field_mean = sum(float(tank.inputs["stoiip"]) for tank in cfg.tanks)
    all_fixed = all(tank.p90_stoiip is None for tank in cfg.tanks)
    rows.append(
        {
            "variable": "stoiip_total",
            "target_P90": field_mean if all_fixed else float("nan"),
            "target_mean": field_mean,
            "target_P10": field_mean if all_fixed else float("nan"),
            **_statistics(data["stoiip_total"]),
            **_prior_columns(None),
        }
    )

    successful = data.loc[data["status"] == "ok"] if "status" in data else data
    for name in cfg.results.read:
        if name not in successful.columns:
            continue
        stats = _statistics(successful[name])
        if stats["n"] == 0:
            continue
        rows.append(
            {
                "variable": name,
                "target_P90": float("nan"),
                "target_mean": float("nan"),
                "target_P10": float("nan"),
                **stats,
                **_prior_columns(None),
            }
        )

    summary = pd.DataFrame(rows)
    path = Path(cfg.out_dir) / "ensemble_summary.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(path, index=False)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sample mean/P90/P10 tank volumes and rerun the verified simple "
            "MBAL prediction for every realization."
        )
    )
    parser.add_argument("config", help="the same YAML used by mbal_simple.py")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="write samples and summary; never open MBAL",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="write each sampled volume set and run the prediction",
    )
    parser.add_argument("--n", type=int, help="override n_realizations")
    parser.add_argument("--seed", type=int, help="override the random seed")
    parser.add_argument("--sampling", choices=("lhs", "mc"))
    parser.add_argument("--out-dir")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="discard prior ensemble result/profile CSVs instead of resuming",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = simple.load_config(args.config)
    updates: dict[str, Any] = {}
    if args.n is not None:
        if args.n <= 0:
            raise ValueError("--n must be greater than zero")
        updates["n_realizations"] = args.n
    if args.seed is not None:
        updates["seed"] = args.seed
    if args.sampling is not None:
        updates["sampling"] = args.sampling
    if args.out_dir is not None:
        updates["out_dir"] = args.out_dir
    if updates:
        cfg = replace(cfg, **updates)

    if all(tank.p90_stoiip is None for tank in cfg.tanks):
        message = (
            f"no tank in {args.config} has p90_stoiip/p10_stoiip, so every "
            "realization holds the same volumes. Add the O&G low/high anchors "
            "to the tanks whose volume is uncertain."
        )
        if args.run:
            print(
                f"error: {message} Refusing to run {cfg.n_realizations} "
                "identical predictions.",
                file=sys.stderr,
            )
            return 2
        print(f"warning: {message}", file=sys.stderr)

    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for name in ("ensemble_results.csv", "ensemble_profiles.csv"):
            path = out_dir / name
            if path.exists():
                path.unlink()

    samples = build_sample_table(cfg)
    sample_path = out_dir / "ensemble_samples.csv"
    samples.to_csv(sample_path, index=False)
    print(
        f"sampled {len(samples)} realizations ({cfg.sampling.upper()}, "
        f"seed={cfg.seed}) -> {sample_path}"
    )

    if args.dry_run:
        summary = summarize(samples, cfg)
        print(summary.round(4).to_string(index=False))
        print("dry run only; MBAL was not opened")
        return 0

    server = simple.OpenServer(cfg.prog_id)
    results = run_ensemble(samples, cfg, server)
    summary = summarize(results, cfg)
    print(summary.round(4).to_string(index=False))
    ok = int((results["status"] == "ok").sum())
    print(f"{ok}/{len(results)} realizations succeeded")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
