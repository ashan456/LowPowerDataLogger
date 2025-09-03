# PlotterV1.py
import argparse, os, sys
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# --- helpers ---------------------------------------------------------------
def pick_col(df, candidates, required=True):
    """Pick the first existing column name from candidates (case-insensitive)."""
    cols = {c.lower(): c for c in df.columns}
    for cand in candidates:
        key = cand.lower()
        if key in cols:
            return cols[key]
    if required:
        raise KeyError(
            f"None of these columns were found: {candidates}. "
            f"Available: {list(df.columns)}"
        )
    return None

def read_csv_relaxed(path):
    # Try common separators; trim header whitespace
    try:
        df = pd.read_csv(path)
    except Exception:
        df = pd.read_csv(path, sep=";")
    df.columns = [c.strip() for c in df.columns]
    return df

# --- plotting --------------------------------------------------------------
def plot_logs(date_str, logs_dir="."):
    filename = os.path.join(logs_dir, f"{date_str}.csv")
    if not os.path.exists(filename):
        print(f"❌ File not found: {filename}")
        sys.exit(1)

    df = read_csv_relaxed(filename)

    # Map flexible column names
    time_col = pick_col(df, ["time", "timestamp"])
    temp_col = pick_col(df, ["temperature", "temperatu", "temp", "t"])
    hum_col  = pick_col(df, ["humidity", "hum", "rh"])
    vbat_col = pick_col(df, ["vbat_mV", "vbat_mv", "vbat", "battery_mv", "battery"])

    # Parse time (HH:MM:SS). Keep as datetime for plotting
    try:
        t = pd.to_datetime(df[time_col].astype(str), format="%H:%M:%S", errors="coerce")
    except Exception:
        t = pd.to_datetime(df[time_col].astype(str), errors="coerce")
    df = df.assign(_t=t).dropna(subset=["_t"]).sort_values("_t")

    # Ensure numeric
    for c in [temp_col, hum_col, vbat_col]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # --- build figure -------------------------------------------------------
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 8), sharex=True)

    # Same major/minor locators & formatter for ALL axes -> aligned vertical gridlines
    major_locator = mdates.AutoDateLocator()
    major_formatter = mdates.DateFormatter("%H:%M")
    # A slightly denser minor locator to show faint intermediate guides
    minor_locator = mdates.AutoDateLocator(minticks=15, maxticks=30)

    for ax in (ax1, ax2, ax3):
        ax.xaxis.set_major_locator(major_locator)
        ax.xaxis.set_major_formatter(major_formatter)
        ax.xaxis.set_minor_locator(minor_locator)
        # Grid: vertical lines for both major & minor ticks; horizontal too for readability
        ax.grid(True, which="major", axis="both", linewidth=0.8)
        ax.grid(True, which="minor", axis="x", linewidth=0.5, alpha=0.35)

    # Temperature (red)
    ax1.plot(df["_t"], df[temp_col], color="red", marker="o", linestyle="-")
    ax1.set_ylabel("Temperature (°C)")

    # Humidity (green)
    ax2.plot(df["_t"], df[hum_col], color="green", marker="o", linestyle="-")
    ax2.set_ylabel("Humidity (%)")

    # Battery (blue)
    ax3.plot(df["_t"], df[vbat_col], color="blue", marker="o", linestyle="-")
    ax3.set_ylabel("Battery (mV)")

    # Common X label and pretty tick labels
    plt.setp(ax1.get_xticklabels(), rotation=45, ha="right")
    plt.setp(ax2.get_xticklabels(), rotation=45, ha="right")
    plt.setp(ax3.get_xticklabels(), rotation=45, ha="right")
    fig.text(0.5, 0.04, "Time (24Hrs)", ha="center", fontsize=12)

    plt.suptitle(f"Data Log for {date_str}", fontsize=14)
    plt.tight_layout(rect=[0, 0.06, 1, 0.95])
    plt.show()

# --- cli -------------------------------------------------------------------
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Plot logger data for a given date.")
    p.add_argument("--date", required=True, help="YYYY-MM-DD (e.g. 2025-08-31)")
    p.add_argument("--dir", default=".", nargs="?", help="Logs directory; default is current folder")
    args = p.parse_args()
    plot_logs(args.date, args.dir)
