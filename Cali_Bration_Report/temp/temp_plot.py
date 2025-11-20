import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

# =================== SETTINGS ===================
CSV_FILE = Path("temp_cal.csv")   # your file name
REF_COL = "Reference"
DEV_COL = "Device"
# =================================================

def main():
    # ---- 1) Read data ----
    df = pd.read_csv(CSV_FILE)

    # Normalize column names (strip spaces etc.)
    df.columns = [str(c).strip() for c in df.columns]

    # If the expected names are not found, assume first two columns
    if REF_COL not in df.columns or DEV_COL not in df.columns:
        df = df.iloc[:, :2].copy()
        df.columns = [REF_COL, DEV_COL]

    # x = device reading, y = reference temperature
    x = df[DEV_COL].to_numpy(dtype=float)   # Device
    y = df[REF_COL].to_numpy(dtype=float)   # Reference

    # ---- 2) Fit linear model: Reference = a * Device + C ----
    a, C = np.polyfit(x, y, 1)
    y_fit = a * x + C   # calibrated/reference estimate for each device reading

    # ---- 3) Regression + residual statistics ----
    residuals = y - y_fit          # Reference - Predicted Reference
    n = len(x)

    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot

    mean_res = np.mean(residuals)
    std_res = np.std(residuals, ddof=1)   # sample std
    rmse = np.sqrt(ss_res / n)
    max_abs_res = np.max(np.abs(residuals))

    # ---- 4) Print stats to shell ----
    print("=== Temperature Calibration: Reference = a * Device + C ===")
    print(f"Number of points        : {n}")
    print(f"Slope (a)               : {a:.8f}")
    print(f"Intercept (C)           : {C:.8f}")
    print("Model (use this to calibrate):")
    print(f"  T_calibrated = {a:.8f} * T_device + {C:.8f}")
    print(f"R²                      : {r2:.8f}")
    print()
    print("=== Residual statistics (Reference - Calibrated) ===")
    print(f"Mean residual           : {mean_res:.6f} °C")
    print(f"Std deviation           : {std_res:.6f} °C")
    print(f"RMSE                    : {rmse:.6f} °C")
    print(f"Max |residual|          : {max_abs_res:.6f} °C")

    # ---- 5) Plot data + fitted line ----
    fig, ax = plt.subplots()

    # Scatter of measured points
    ax.scatter(x, y, label="Lab data", marker="o")

    # Fitted line over full range of device readings
    x_line = np.linspace(x.min(), x.max(), 200)
    y_line = a * x_line + C
    ax.plot(x_line, y_line, label="Linear fit", linewidth=1,color="red")

    ax.set_xlabel("Device temperature (°C)")
    ax.set_ylabel("Reference temperature (°C)")
    ax.set_title("Temperature Calibration (Reference vs Device)")

    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()

    # # Put equation and R² on the plot
    # eq_text = (
        # f"T_cal = {a:.6f} · T_device + {C:.6f}\n"
        # f"R² = {r2:.6f}"
    # )
    # ax.text(
        # 0.05, 0.95, eq_text,
        # transform=ax.transAxes,
        # va="top", ha="left",
        # bbox=dict(boxstyle="round", alpha=0.2)
    # )

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
