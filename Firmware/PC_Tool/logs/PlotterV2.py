#!/usr/bin/env python3
"""
PlotterV2.py — robust CLI to visualize logger CSVs (Temp, RH, VBAT).
- Smarter CSV ingest (auto delimiter, comments, decimal, gz support)
- Flexible column detection & manual mapping
- Optional subplots (hide missing series), or compact one-figure mode
- Resample, rolling average, downsampling for huge files
- Time range cropping; date+time combining; tolerant parsing
- Save to PNG/PDF; dark/light styles; optional markers
- VBAT in V or mV; threshold shading; SOC curve mapping
- Stats printout; min/max markers; event markers from CSV
"""

import argparse, sys, os, json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ------------------------------- Column picking -------------------------------
DEFAULT_CANDIDATES = {
    "time": ["time", "timestamp", "t", "Time", "TIME"],
    "temp": ["temperature", "temp_c", "temp", "t_c", "T_C", "T(C)"],
    "rh":   ["humidity", "rh", "rh_%", "RH", "RH_%"],
    "vbat": ["vbat_mv", "battery_mv", "vbat", "VBAT_mV", "VBAT", "battery"]
}


def pick_col(df: pd.DataFrame, candidates: List[str], required: bool = True) -> Optional[str]:
    cols = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cols[cand.lower()]
    if required:
        raise KeyError(f"None of the columns found: {candidates}. Available: {list(df.columns)}")
    return None


# ------------------------------- CSV reader -----------------------------------
def read_csv_smart(path: Path, decimal: str = ".", comments: str = "#") -> pd.DataFrame:
    """Auto-detect delimiter and support gz/zip. Strip header whitespace."""
    try:
        df = pd.read_csv(path, sep=None, engine="python", comment=comments, decimal=decimal)
    except Exception:
        # Fallback to semicolon
        df = pd.read_csv(path, sep=";", comment=comments, decimal=decimal)
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ------------------------------- Time parsing ---------------------------------
def parse_time(df: pd.DataFrame, time_col: str, date_col: Optional[str] = None) -> pd.Series:
    """
    Parse time column robustly. Accepts HH:MM, HH:MM:SS, or full timestamps.
    If date_col present, combine to full datetime so cross-midnight sorts well.
    """
    t_raw = df[time_col].astype(str)
    if date_col:
        d_raw = pd.to_datetime(df[date_col].astype(str), errors="coerce")
        # combine: use string to preserve the original wall-clock time
        combo = np.where(
            d_raw.notna(), d_raw.dt.strftime("%Y-%m-%d") + " " + t_raw, t_raw
        )
        t = pd.to_datetime(combo, errors="coerce")
    else:
        t = pd.to_datetime(t_raw, errors="coerce", infer_datetime_format=True)
        # If still all NaT, try times only with today's date
        if t.isna().all():
            t = pd.to_datetime("1970-01-01 " + t_raw, errors="coerce")
    return t


# ------------------------------- Utilities ------------------------------------
def coerce_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def parse_col_overrides(arg: Optional[str]) -> Dict[str, str]:
    """
    --cols "time=Time,temp=T,rh=H,vbat=VBAT_mV"
    """
    mapping = {}
    if not arg:
        return mapping
    for pair in arg.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            mapping[k.strip().lower()] = v.strip()
    return mapping


def parse_ylim(arg: Optional[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """
    --y "temp=15:40 rh=20:95 vbat=3200:4200"
    """
    out = {}
    if not arg:
        return out
    for token in arg.split():
        if "=" in token and ":" in token:
            name, rng = token.split("=", 1)
            a, b = rng.split(":", 1)
            lo = float(a) if a else None
            hi = float(b) if b else None
            out[name.strip().lower()] = (lo, hi)
    return out


def maybe_resample(df: pd.DataFrame, rule: Optional[str]) -> pd.DataFrame:
    if not rule:
        return df
    if "_t" not in df.columns:
        return df
    return (df.set_index("_t").resample(rule).mean(numeric_only=True).reset_index())


def maybe_rolling(df: pd.DataFrame, window: Optional[int], cols: List[str]) -> pd.DataFrame:
    if not window or window <= 1:
        return df
    for c in cols:
        if c in df.columns:
            df[c] = df[c].rolling(window, min_periods=1).mean()
    return df


def crop_time(df: pd.DataFrame, timerange: Optional[str]) -> pd.DataFrame:
    if not timerange or "_t" not in df.columns:
        return df
    try:
        a, b = [x.strip() for x in timerange.split(",", 1)]
        t0 = pd.to_datetime(a)
        t1 = pd.to_datetime(b)
        return df[(df["_t"] >= t0) & (df["_t"] <= t1)]
    except Exception:
        return df


def load_events(path: Optional[str]) -> Optional[pd.DataFrame]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"⚠️  Events file not found: {p}")
        return None
    try:
        ev = read_csv_smart(p)
        # Expect columns: time,label (plus optional date)
        tcol = pick_col(ev, ["time", "timestamp", "event_time"])
        dcol = pick_col(ev, ["date", "day", "Date"], required=False)
        ev["_t"] = parse_time(ev, tcol, dcol)
        ev["label"] = ev.get("label", ev.columns[-1]).astype(str)  # attempt
        ev = ev.dropna(subset=["_t"])
        return ev[["_t", "label"]]
    except Exception as e:
        print(f"⚠️  Failed to load events: {e}")
        return None


def infer_cols(df: pd.DataFrame, candidates: Dict[str, List[str]], overrides: Dict[str, str]) -> Dict[str, Optional[str]]:
    out = {}
    # date column is optional but helpful if present
    date_col = None
    for name in ["date", "day", "Date", "DATE"]:
        if name in df.columns:
            date_col = name
            break

    for key in ["time", "temp", "rh", "vbat"]:
        if key in overrides:
            out[key] = overrides[key]
            continue
        try:
            out[key] = pick_col(df, candidates[key], required=(key == "time"))
        except KeyError:
            out[key] = None
    out["date"] = date_col
    return out


def print_stats(df: pd.DataFrame, cols: List[str]):
    print("\n=== Stats ===")
    for c in cols:
        if c in df.columns:
            s = df[c].dropna()
            if not s.empty:
                print(f"{c:>8s} | min={s.min():.3f}  max={s.max():.3f}  mean={s.mean():.3f}  n={len(s)}")
            else:
                print(f"{c:>8s} | (no data)")
        else:
            print(f"{c:>8s} | (missing column)")


# ------------------------------- Plotting -------------------------------------
def plot_data(df: pd.DataFrame,
              title: str,
              style: Optional[str] = None,
              one_figure: bool = False,
              show_markers: bool = True,
              ylims: Optional[Dict[str, Tuple[Optional[float], Optional[float]]]] = None,
              vbat_in_volts: bool = False,
              vbat_threshold: Optional[float] = None,
              soc_curve_path: Optional[str] = None,
              events: Optional[pd.DataFrame] = None,
              save_path: Optional[str] = None,
              dpi: int = 150):
    if style == "dark":
        plt.style.use("dark_background")
    elif style == "light":
        plt.style.use("default")

    # Convert VBAT
    ycols = []
    if "temp" in df.columns: ycols.append("temp")
    if "rh" in df.columns:   ycols.append("rh")
    if "vbat" in df.columns: ycols.append("vbat")

    df_plot = df.copy()

    if "vbat" in df_plot.columns and vbat_in_volts:
        df_plot["vbat"] = df_plot["vbat"] / 1000.0

    # SOC mapping
    soc_col = None
    if "vbat" in df_plot.columns and soc_curve_path:
        try:
            curve = json.loads(Path(soc_curve_path).read_text())
            # Expect dict of "mV"→"%"
            xs = np.array(sorted(map(float, curve.keys())))
            ys = np.array([float(curve[str(int(x))]) if str(int(x)) in curve else float(curve[str(x)]) for x in xs])
            # Work in mV regardless of v units:
            v_mv = df["vbat"].to_numpy(dtype=float)
            soc = np.interp(v_mv, xs, ys, left=ys.min(), right=ys.max())
            df_plot["soc"] = soc
            soc_col = "soc"
        except Exception as e:
            print(f"⚠️  Failed to apply SOC curve: {e}")

    # Figure layout
    marker = "." if show_markers else None

    if one_figure:
        fig, ax = plt.subplots(figsize=(12, 6), constrained_layout=True)
        # Left y: temp & rh (if present)
        lns = []
        labels = []
        if "temp" in df_plot.columns:
            ln1, = ax.plot(df_plot["_t"], df_plot["temp"], marker=marker, linestyle="-", label="Temp (°C)")
            lns.append(ln1); labels.append("Temp (°C)")
        if "rh" in df_plot.columns:
            ln2, = ax.plot(df_plot["_t"], df_plot["rh"], marker=marker, linestyle="-", label="RH (%)")
            lns.append(ln2); labels.append("RH (%)")
        ax2 = ax.twinx()
        if "vbat" in df_plot.columns:
            unit = "V" if vbat_in_volts else "mV"
            ln3, = ax2.plot(df_plot["_t"], df_plot["vbat"], marker=marker, linestyle="-", label=f"VBAT ({unit})")
            lns.append(ln3); labels.append(f"VBAT ({unit})")
        if soc_col:
            ln4, = ax2.plot(df_plot["_t"], df_plot[soc_col], linestyle="--", label="SOC (%)")
            lns.append(ln4); labels.append("SOC (%)")

        ax.set_xlabel("Time")
        ax.set_ylabel("Temp / RH")
        ax2.set_ylabel("VBAT / SOC")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.grid(True, which="both", alpha=0.3)
        labs = [l.get_label() for l in lns]
        ax.legend(lns, labs, loc="best")

        # y-lims
        if ylims:
            if "temp" in ylims and any(ylims["temp"]):
                ax.set_ylim(*ylims["temp"])
            if "rh" in ylims and any(ylims["rh"]):
                ax.set_ylim(*ylims["rh"])
            key = "vbat"
            if soc_col:
                key = "soc" if "soc" in ylims else "vbat"
            if key in ylims and any(ylims[key]):
                ax2.set_ylim(*ylims[key])

        # Threshold shading for VBAT
        if vbat_threshold is not None and "vbat" in df_plot.columns:
            thr = vbat_threshold if not vbat_in_volts else vbat_threshold
            y0, y1 = ax2.get_ylim()
            ax2.axhspan(min(thr, y1), thr, facecolor="red", alpha=0.1)

        # Events
        if events is not None and not events.empty:
            for _, row in events.iterrows():
                ax.axvline(row["_t"], color="tab:gray", alpha=0.3, linestyle=":")
                ax.text(row["_t"], ax.get_ylim()[1], str(row["label"]), rotation=90,
                        va="bottom", fontsize=8, alpha=0.7)

        plt.title(title)

    else:
        # Up to three subplots, only for existing series
        n = sum([c in df_plot.columns for c in ["temp", "rh", "vbat"]])
        if n == 0:
            print("No plottable series found.")
            return
        fig, axes = plt.subplots(n, 1, figsize=(12, 8), sharex=True)
        if n == 1:
            axes = [axes]

        idx = 0
        if "temp" in df_plot.columns:
            ax = axes[idx]; idx += 1
            ax.plot(df_plot["_t"], df_plot["temp"], marker=marker, linestyle="-")
            ax.set_ylabel("Temp (°C)")
            if ylims and "temp" in ylims and any(ylims["temp"]):
                ax.set_ylim(*ylims["temp"])
            ax.grid(True, alpha=0.3)

        if "rh" in df_plot.columns:
            ax = axes[idx]; idx += 1
            ax.plot(df_plot["_t"], df_plot["rh"], marker=marker, linestyle="-")
            ax.set_ylabel("RH (%)")
            if ylims and "rh" in ylims and any(ylims["rh"]):
                ax.set_ylim(*ylims["rh"])
            ax.grid(True, alpha=0.3)

        if "vbat" in df_plot.columns:
            ax = axes[idx]; idx += 1
            unit = "V" if vbat_in_volts else "mV"
            ax.plot(df_plot["_t"], df_plot["vbat"], marker=marker, linestyle="-")
            ax.set_ylabel(f"VBAT ({unit})")
            if ylims and "vbat" in ylims and any(ylims["vbat"]):
                ax.set_ylim(*ylims["vbat"])
            if vbat_threshold is not None:
                ax.axhline(vbat_threshold, color="red", linestyle="--", alpha=0.3, label="VBAT threshold")
                ax.legend(loc="best")
            ax.grid(True, alpha=0.3)

        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.suptitle(title)
        fig.align_ylabels(axes)

        # Events on all panels
        if events is not None and not events.empty:
            for ax in axes:
                for _, row in events.iterrows():
                    ax.axvline(row["_t"], color="tab:gray", alpha=0.3, linestyle=":")
                    ax.text(row["_t"], ax.get_ylim()[1], str(row["label"]), rotation=90,
                            va="bottom", fontsize=8, alpha=0.7)

        plt.xlabel("Time (24Hrs)")

    plt.tight_layout()
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(save_path, dpi=dpi, bbox_inches="tight")
        print(f"💾 Saved: {save_path}")
        plt.close()
    else:
        plt.show()


# ------------------------------- Main pipeline --------------------------------
def load_and_prepare(paths: List[Path],
                     decimal: str,
                     col_overrides: Dict[str, str],
                     resample: Optional[str],
                     rolling: Optional[int],
                     time_range: Optional[str]) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
    # Load and concatenate
    dfs = []
    for p in paths:
        df = read_csv_smart(p, decimal=decimal)
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True, copy=False)

    # Infer columns
    cols = infer_cols(df, DEFAULT_CANDIDATES, col_overrides)
    tcol = cols.get("time")
    dcol = cols.get("date")

    # Parse time and sort
    df["_t"] = parse_time(df, tcol, dcol)
    # coerce numeric
    for key, name in [("temp", cols.get("temp")), ("rh", cols.get("rh")), ("vbat", cols.get("vbat"))]:
        if name and name in df.columns:
            df[key] = coerce_numeric(df[name])

    # Drop rows where time is NaT or *all* series NaN
    present_cols = [c for c in ["temp", "rh", "vbat"] if c in df.columns]
    df = df.dropna(subset=["_t"])
    df = df.dropna(subset=present_cols, how="all")
    df = df.sort_values("_t")

    # Resample / Rolling / Crop
    df = maybe_resample(df, resample)
    df = maybe_rolling(df, rolling, present_cols)
    df = crop_time(df, time_range)

    return df, cols


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plot logger CSVs with flexible options.")
    ap.add_argument("--date", help="YYYY-MM-DD (used with --dir to pick a single file)")
    ap.add_argument("--dir", default=".", help="Folder with logs (used with --date)")
    ap.add_argument("--files", nargs="*", help="Explicit CSV files to load (can mix .csv and .csv.gz)")
    ap.add_argument("--decimal", default=".", help="Decimal separator ('.' or ',')")
    ap.add_argument("--cols", help="Manual column map, e.g. time=Time,temp=T,rh=H,vbat=VBAT_mV")
    ap.add_argument("--style", choices=["light", "dark"], help="Matplotlib style")
    ap.add_argument("--resample", help="Pandas resample rule, e.g. '1min', '5min'")
    ap.add_argument("--rolling", type=int, help="Window size for moving average")
    ap.add_argument("--time-range", help="Crop to time range: '2025-08-31 09:00,2025-08-31 17:30'")
    ap.add_argument("--no-markers", action="store_true", help="Disable point markers for speed")
    ap.add_argument("--one-figure", action="store_true", help="Single figure with twin y-axis")
    ap.add_argument("--save", help="Save to path (.png/.pdf) instead of showing")
    ap.add_argument("--dpi", type=int, default=150, help="Save DPI (default 150)")
    ap.add_argument("--title", help="Custom plot title")
    ap.add_argument("--y", help="YAxis limits, e.g. 'temp=15:40 rh=20:95 vbat=3200:4200'")
    ap.add_argument("--vbat-v", action="store_true", help="Show battery in Volts (default mV)")
    ap.add_argument("--vbat-th", type=float, help="Battery threshold (mV if --vbat-v is off; V if on)")
    ap.add_argument("--events", help="CSV with event time/label columns to mark vertical lines")
    ap.add_argument("--soc", help="JSON file mapping VBAT mV to SOC percent for derived curve")

    args = ap.parse_args(argv)

    # Determine input files
    files: List[Path] = []
    if args.files:
        files = [Path(f) for f in args.files]
    elif args.date:
        f = Path(args.dir) / f"{args.date}.csv"
        files = [f]
    else:
        print("❌ Provide either --files or --date (with --dir).")
        return 2

    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print("❌ Missing files:\n  - " + "\n  - ".join(missing))
        return 2

    col_overrides = parse_col_overrides(args.cols)
    ylims = parse_ylim(args.y)

    df, cols = load_and_prepare(
        files,
        decimal=args.decimal,
        col_overrides=col_overrides,
        resample=args.resample,
        rolling=args.rolling,
        time_range=args.time_range
    )

    # Stats
    present_cols = [c for c in ["temp", "rh", "vbat"] if c in df.columns]
    print_stats(df, present_cols)

    # Events
    ev = load_events(args.events)

    # Title
    title = args.title or (
        f"Logger Plot — {', '.join([f.name for f in files])}"
    )

    plot_data(
        df=df,
        title=title,
        style=args.style,
        one_figure=args.one_figure,
        show_markers=not args.no_markers,
        ylims=ylims,
        vbat_in_volts=args.vbat_v,
        vbat_threshold=args.vbat_th,
        soc_curve_path=args.soc,
        events=ev,
        save_path=args.save,
        dpi=args.dpi
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
