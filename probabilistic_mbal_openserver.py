"""
Probabilistic MBAL via Petroleum Experts OpenServer
===================================================

Model assumed: one MBAL material-balance model containing TWO tanks that are
produced through a SINGLE well (either two tanks completed in the same string,
or two tanks linked by transmissibility and drained by one well).

Monte Carlo scheme
------------------
    STOIIP_total  ~ user distribution (lognormal from P90/P10, or triangular/uniform)
    f_A           ~ user distribution (fraction of total STOIIP assigned to Tank A)
    STOIIP_A = f_A * STOIIP_total
    STOIIP_B = (1 - f_A) * STOIIP_total

Optionally, per-tank aquifer multipliers are also sampled so the split of
production between the tanks is not purely volumetric.

For every realization the script:
    1. writes the tank volumes (and optional aquifer params) into MBAL,
    2. runs the MBAL prediction,
    3. reads back per-tank results (cum oil, average pressure, recovery factor),
    4. appends one row to a CSV (crash-safe: you can restart and it resumes).

Finally it reports P90 / P50 / P10 per tank and writes plots.

IMPORTANT — OPENSERVER TAG STRINGS
----------------------------------
The exact OpenServer variable strings for MBAL change between IPM versions.
DO NOT trust the defaults in the TAGS block below blindly. Get the real ones from
MBAL itself: open the model, navigate to the field you want, and use the
OpenServer variable browser / right-click "Copy variable name" — that gives you
the exact string for your version. Then paste it into TAGS. Everything else in
this script is version independent.

Usage
-----
    python probabilistic_mbal_openserver.py --dry-run        # sampling only, no MBAL, no licence
    python probabilistic_mbal_openserver.py --n 500          # full run
    python probabilistic_mbal_openserver.py --summarize-only # re-plot from existing CSV

Requires: numpy, pandas, matplotlib, (scipy optional for LHS), pywin32 (Windows only).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- 
# 1. CONFIGURATION
# -----------------------------------------------------------------------------

@dataclass
class Config:
    # --- model -------------------------------------------------------------
    mbal_file: str = r"C:\Work\Models\two_tank_model.mbi"
    tank_names: tuple = ("Tank A", "Tank B")      # for labelling only
    tank_indices: tuple = (0, 1)                  # OpenServer 0-based tank index
    unit_stoiip: str = "MMstb"                    # unit qualifier passed to OpenServer
    unit_press: str = "psig"
    unit_cum: str = "MMstb"

    # --- Monte Carlo -------------------------------------------------------
    n_realizations: int = 200
    seed: int = 42
    sampling: str = "lhs"                         # "lhs" or "mc"

    # --- total STOIIP distribution (field total, both tanks) ---------------
    # O&G convention: P90 = low case, P10 = high case.
    stoiip_dist: str = "lognormal"                # lognormal | triangular | uniform
    stoiip_p90: float = 45.0                      # MMstb
    stoiip_p10: float = 120.0                     # MMstb
    stoiip_min: float = 40.0                      # used by triangular/uniform
    stoiip_mode: float = 75.0
    stoiip_max: float = 130.0

    # --- split between tanks: fraction of total STOIIP in Tank A -----------
    split_dist: str = "triangular"                # triangular | uniform | beta
    split_min: float = 0.30
    split_mode: float = 0.50
    split_max: float = 0.70
    split_beta_a: float = 5.0                     # used if split_dist == "beta"
    split_beta_b: float = 5.0

    # --- optional extra uncertainty: aquifer volume multiplier per tank ----
    sample_aquifer: bool = False
    aq_mult_min: float = 0.5
    aq_mult_max: float = 3.0

    # --- run control -------------------------------------------------------
    out_csv: str = "mbal_mc_results.csv"
    out_dir: str = "mbal_mc_output"
    run_timeout_s: float = 600.0                  # guard for a hung prediction
    stop_on_error: bool = False                   # False = log failure, keep going


CFG = Config()

# -----------------------------------------------------------------------------
# 2. OPENSERVER TAG STRINGS  <-- VERIFY THESE AGAINST YOUR IPM VERSION
# -----------------------------------------------------------------------------
# {i} is substituted with the tank index, {u} with the unit qualifier.
# Unit qualifiers are optional; if you drop them OpenServer uses the app's
# current unit system, which is a classic source of silent 1e3/1e6 errors.

TAGS = {
    # --- inputs ---
    "tank_stoiip":   'MBAL.MB[0].TANK[{i}].OIIP("{u}")',
    "aquifer_mult":  'MBAL.MB[0].TANK[{i}].AQUIFER.VOLRATIO',

    # --- commands ---
    "cmd_open":      'MBAL.OPENFILE("{path}")',
    "cmd_run_pred":  'MBAL.MB[0].PREDICTION.CALCULATE',
    "cmd_close":     'MBAL.SHUTDOWN',

    # --- outputs (last prediction timestep, per tank) ---
    # If your version only exposes indexed timesteps, set n_steps via
    # "res_nsteps" and read the last one.
    "res_nsteps":    'MBAL.MB[0].PREDICTION.RESULTS[{i}].COUNT',
    "res_cumoil":    'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMOIL("{u}")',
    "res_pressure":  'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].PRESSURE("{u}")',
    "res_cumwat":    'MBAL.MB[0].PREDICTION.RESULTS[{i}][{k}].CUMWATER("{u}")',
}

# -----------------------------------------------------------------------------
# 3. SAMPLING
# -----------------------------------------------------------------------------

def lognormal_from_p90_p10(p90: float, p10: float) -> tuple[float, float]:
    """Return (mu, sigma) of ln(X) for a lognormal matching the P90 (low) and
    P10 (high) exceedance values used in O&G practice."""
    z = 1.2815515655446004  # standard normal 90th percentile
    sigma = math.log(p10 / p90) / (2.0 * z)
    mu = 0.5 * math.log(p10 * p90)
    return mu, sigma


def _norm_ppf(u: np.ndarray) -> np.ndarray:
    try:
        from scipy.stats import norm
        return norm.ppf(u)
    except ImportError:
        # Acklam rational approximation, plenty accurate for MC input sampling
        a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
             3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        u = np.clip(u, 1e-12, 1 - 1e-12)
        out = np.empty_like(u)
        lo, hi = u < plow, u > phigh
        mid = ~(lo | hi)
        q = np.sqrt(-2 * np.log(u[lo]))
        out[lo] = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                  ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        q = np.sqrt(-2 * np.log(1 - u[hi]))
        out[hi] = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        q = u[mid] - 0.5
        r = q * q
        out[mid] = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
                   (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
        return out


def _tri_ppf(u: np.ndarray, lo: float, mode: float, hi: float) -> np.ndarray:
    c = (mode - lo) / (hi - lo)
    out = np.where(
        u < c,
        lo + np.sqrt(np.maximum(u * (hi - lo) * (mode - lo), 0.0)),
        hi - np.sqrt(np.maximum((1 - u) * (hi - lo) * (hi - mode), 0.0)),
    )
    return out


def _beta_ppf(u: np.ndarray, a: float, b: float) -> np.ndarray:
    try:
        from scipy.stats import beta
        return beta.ppf(u, a, b)
    except ImportError:
        raise RuntimeError("split_dist='beta' needs scipy installed")


def unit_hypercube(n: int, d: int, cfg: Config) -> np.ndarray:
    """n x d matrix of U(0,1) samples, Latin Hypercube if requested/available."""
    rng = np.random.default_rng(cfg.seed)
    if cfg.sampling.lower() == "lhs":
        try:
            from scipy.stats.qmc import LatinHypercube
            return LatinHypercube(d=d, seed=cfg.seed).random(n)
        except ImportError:
            # manual LHS
            u = np.empty((n, d))
            for j in range(d):
                perm = rng.permutation(n)
                u[:, j] = (perm + rng.random(n)) / n
            return u
    return rng.random((n, d))


def build_sample_table(cfg: Config) -> pd.DataFrame:
    d = 2 + (2 if cfg.sample_aquifer else 0)
    u = unit_hypercube(cfg.n_realizations, d, cfg)

    # --- total STOIIP ---
    if cfg.stoiip_dist == "lognormal":
        mu, sigma = lognormal_from_p90_p10(cfg.stoiip_p90, cfg.stoiip_p10)
        stoiip = np.exp(mu + sigma * _norm_ppf(u[:, 0]))
    elif cfg.stoiip_dist == "triangular":
        stoiip = _tri_ppf(u[:, 0], cfg.stoiip_min, cfg.stoiip_mode, cfg.stoiip_max)
    elif cfg.stoiip_dist == "uniform":
        stoiip = cfg.stoiip_min + u[:, 0] * (cfg.stoiip_max - cfg.stoiip_min)
    else:
        raise ValueError(f"unknown stoiip_dist {cfg.stoiip_dist}")

    # --- split fraction to Tank A ---
    if cfg.split_dist == "triangular":
        fa = _tri_ppf(u[:, 1], cfg.split_min, cfg.split_mode, cfg.split_max)
    elif cfg.split_dist == "uniform":
        fa = cfg.split_min + u[:, 1] * (cfg.split_max - cfg.split_min)
    elif cfg.split_dist == "beta":
        fa = cfg.split_min + _beta_ppf(u[:, 1], cfg.split_beta_a, cfg.split_beta_b) * \
             (cfg.split_max - cfg.split_min)
    else:
        raise ValueError(f"unknown split_dist {cfg.split_dist}")

    df = pd.DataFrame({
        "realization": np.arange(cfg.n_realizations),
        "stoiip_total": stoiip,
        "frac_A": fa,
        "stoiip_A": stoiip * fa,
        "stoiip_B": stoiip * (1.0 - fa),
    })

    if cfg.sample_aquifer:
        lo, hi = cfg.aq_mult_min, cfg.aq_mult_max
        df["aq_mult_A"] = lo + u[:, 2] * (hi - lo)
        df["aq_mult_B"] = lo + u[:, 3] * (hi - lo)

    return df


# -----------------------------------------------------------------------------
# 4. OPENSERVER SESSION
# -----------------------------------------------------------------------------

class OpenServer:
    """Thin wrapper around the Petex OpenServer COM object with error checking."""

    def __init__(self, prog_id: str = "PX32.OpenServer.1"):
        import win32com.client  # imported lazily so --dry-run works anywhere
        self.os = win32com.client.Dispatch(prog_id)

    # --- low level -----------------------------------------------------
    def _check(self, code: int, what: str):
        if code:
            try:
                desc = self.os.GetErrorDescription(code)
            except Exception:
                desc = "(no description available)"
            raise RuntimeError(f"OpenServer error {code} on {what}: {desc}")

    def cmd(self, s: str):
        self._check(self.os.DoCommand(s), s)

    def slow_cmd(self, s: str):
        """Use for anything that runs a calculation."""
        self._check(self.os.DoSlowCommand(s), s)

    def set(self, tag: str, value):
        self._check(self.os.DoSet(tag, value), f"DoSet {tag}")

    def get(self, tag: str) -> float:
        val = self.os.DoGet(tag)
        err = self.os.GetLastError("MBAL")
        if err:
            raise RuntimeError(f"OpenServer error reading {tag}: "
                               f"{self.os.GetErrorDescription(err)}")
        return float(val)

    def get_str(self, tag: str) -> str:
        return str(self.os.DoGet(tag))

    # --- context manager ----------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.cmd(TAGS["cmd_close"])
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 5. ONE REALIZATION
# -----------------------------------------------------------------------------

def apply_realization(srv: OpenServer, row: pd.Series, cfg: Config):
    for slot, tank_idx in enumerate(cfg.tank_indices):
        key = "A" if slot == 0 else "B"
        srv.set(TAGS["tank_stoiip"].format(i=tank_idx, u=cfg.unit_stoiip),
                float(row[f"stoiip_{key}"]))
        if cfg.sample_aquifer:
            srv.set(TAGS["aquifer_mult"].format(i=tank_idx),
                    float(row[f"aq_mult_{key}"]))


def read_results(srv: OpenServer, cfg: Config, row: pd.Series) -> dict:
    out = {}
    for slot, tank_idx in enumerate(cfg.tank_indices):
        key = "A" if slot == 0 else "B"
        try:
            n = int(srv.get(TAGS["res_nsteps"].format(i=tank_idx)))
            last = max(n - 1, 0)
        except Exception:
            last = 0  # some versions expose only the final state
        cum = srv.get(TAGS["res_cumoil"].format(i=tank_idx, k=last, u=cfg.unit_cum))
        prs = srv.get(TAGS["res_pressure"].format(i=tank_idx, k=last, u=cfg.unit_press))
        try:
            wat = srv.get(TAGS["res_cumwat"].format(i=tank_idx, k=last, u=cfg.unit_cum))
        except Exception:
            wat = float("nan")
        stoiip = float(row[f"stoiip_{key}"])
        out[f"np_{key}"] = cum
        out[f"pres_{key}"] = prs
        out[f"wp_{key}"] = wat
        out[f"rf_{key}"] = cum / stoiip if stoiip > 0 else float("nan")
    out["np_total"] = out["np_A"] + out["np_B"]
    out["rf_total"] = out["np_total"] / float(row["stoiip_total"])
    return out


def run_monte_carlo(samples: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    os.makedirs(cfg.out_dir, exist_ok=True)
    csv_path = os.path.join(cfg.out_dir, cfg.out_csv)

    done = set()
    if os.path.exists(csv_path):
        prev = pd.read_csv(csv_path)
        done = set(prev["realization"].tolist())
        print(f"Resuming: {len(done)} realizations already in {csv_path}")

    with OpenServer() as srv:
        srv.cmd(TAGS["cmd_open"].format(path=cfg.mbal_file))
        print(f"Opened {cfg.mbal_file}")

        for _, row in samples.iterrows():
            r = int(row["realization"])
            if r in done:
                continue
            rec = row.to_dict()
            t0 = time.time()
            try:
                apply_realization(srv, row, cfg)
                srv.slow_cmd(TAGS["cmd_run_pred"])
                rec.update(read_results(srv, cfg, row))
                rec["status"] = "ok"
            except Exception as e:
                rec["status"] = f"failed: {e}"
                print(f"  [{r}] FAILED: {e}")
                if cfg.stop_on_error:
                    raise
            rec["runtime_s"] = round(time.time() - t0, 2)

            pd.DataFrame([rec]).to_csv(
                csv_path, mode="a", header=not os.path.exists(csv_path), index=False)

            if rec["status"] == "ok":
                print(f"  [{r}] STOIIP {row['stoiip_total']:.1f} "
                      f"({row['frac_A']*100:.0f}% A)  ->  "
                      f"Np_A {rec['np_A']:.2f}  Np_B {rec['np_B']:.2f}  "
                      f"({rec['runtime_s']}s)")

    return pd.read_csv(csv_path)


# -----------------------------------------------------------------------------
# 6. SUMMARY & PLOTS
# -----------------------------------------------------------------------------

def percentiles(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {"P90": np.nan, "P50": np.nan, "P10": np.nan, "mean": np.nan}
    # O&G convention: P90 = low, P10 = high
    return {"P90": np.percentile(x, 10), "P50": np.percentile(x, 50),
            "P10": np.percentile(x, 90), "mean": x.mean()}


def summarize(df: pd.DataFrame, cfg: Config):
    ok = df[df.get("status", "ok") == "ok"] if "status" in df else df
    print(f"\n{len(ok)} successful realizations of {len(df)}\n")

    rows = []
    cols = [("stoiip_A", f"{cfg.tank_names[0]} STOIIP"),
            ("stoiip_B", f"{cfg.tank_names[1]} STOIIP"),
            ("stoiip_total", "Field STOIIP")]
    for c in ("np_A", "np_B", "np_total", "rf_A", "rf_B", "rf_total"):
        if c in ok:
            cols.append((c, c))
    for c, label in cols:
        if c in ok:
            p = percentiles(ok[c].values)
            rows.append({"variable": label, **{k: round(v, 3) for k, v in p.items()}})
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))

    os.makedirs(cfg.out_dir, exist_ok=True)
    summary.to_csv(os.path.join(cfg.out_dir, "summary_percentiles.csv"), index=False)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed - skipping plots)")
        return

    plot_pairs = [("stoiip_A", "stoiip_B", "STOIIP per tank")]
    if "np_A" in ok:
        plot_pairs.append(("np_A", "np_B", "Cumulative oil per tank"))
    if "rf_A" in ok:
        plot_pairs.append(("rf_A", "rf_B", "Recovery factor per tank"))

    for ca, cb, title in plot_pairs:
        fig, ax = plt.subplots(1, 2, figsize=(11, 4))
        for c, name in ((ca, cfg.tank_names[0]), (cb, cfg.tank_names[1])):
            v = ok[c].dropna().values
            ax[0].hist(v, bins=30, alpha=0.55, label=name)
            xs = np.sort(v)
            ax[1].plot(xs, 1.0 - np.arange(1, xs.size + 1) / xs.size, label=name)
        ax[0].set_title(title); ax[0].legend(); ax[0].set_ylabel("count")
        ax[1].set_title("Exceedance (P90 / P50 / P10)")
        ax[1].set_ylabel("P(X > x)"); ax[1].grid(alpha=0.3); ax[1].legend()
        for y in (0.9, 0.5, 0.1):
            ax[1].axhline(y, ls=":", lw=0.8, color="grey")
        fig.tight_layout()
        fname = os.path.join(cfg.out_dir, f"{ca}_{cb}.png")
        fig.savefig(fname, dpi=130); plt.close(fig)
        print(f"wrote {fname}")


# -----------------------------------------------------------------------------
# 7. MAIN
# -----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, help="number of realizations")
    ap.add_argument("--model", help="path to .mbi file")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--dry-run", action="store_true",
                    help="sample and report inputs only; never touches MBAL")
    ap.add_argument("--summarize-only", action="store_true",
                    help="re-read the results CSV and re-plot")
    args = ap.parse_args(argv)

    cfg = CFG
    if args.n:
        cfg.n_realizations = args.n
    if args.model:
        cfg.mbal_file = args.model
    if args.seed is not None:
        cfg.seed = args.seed

    if args.summarize_only:
        path = os.path.join(cfg.out_dir, cfg.out_csv)
        summarize(pd.read_csv(path), cfg)
        return 0

    samples = build_sample_table(cfg)
    print(f"Sampled {len(samples)} realizations "
          f"({cfg.sampling.upper()}, seed={cfg.seed})")
    print(samples.describe().T[["mean", "min", "50%", "max"]].round(2).to_string())

    if args.dry_run:
        os.makedirs(cfg.out_dir, exist_ok=True)
        p = os.path.join(cfg.out_dir, "samples_dry_run.csv")
        samples.to_csv(p, index=False)
        print(f"\nDry run - wrote {p}. No MBAL session was opened.")
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
