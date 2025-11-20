import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# === Load CSV with correct encoding ===
df = pd.read_csv("temp_cal.csv", encoding="cp1252")

# Correct X and Y for calibration model
device = df["Device Reading (°C)"]       # X
ref    = df["Reference Temperature (°C)"]  # Y

# === Linear regression: Reference = m * Device + b  ===
m, b = np.polyfit(device, ref, 1)

# Fit line points
x_fit = np.linspace(device.min(), device.max(), 100)
y_fit = m * x_fit + b

# Compute R²
y_pred = m * device + b
ss_res = np.sum((ref - y_pred)**2)
ss_tot = np.sum((ref - ref.mean())**2)
r2 = 1 - ss_res/ss_tot

print("=== Calibration Regression ===")
print(f"Reference = {m:.6f} * Device + {b:.6f}")
print(f"Slope (m):     {m:.6f}")
print(f"Intercept (b): {b:.6f}")
print(f"R²:            {r2:.6f}")

# === Plot ===
plt.figure(figsize=(8, 6))

# Scatter: measured data
plt.scatter(device, ref, s=70, label="Calibration Data")

# Regression line
plt.plot(x_fit, y_fit, label=f"Fit: ref = {m:.4f}·dev + {b:.4f}\n(R²={r2:.4f})")

# Ideal line (optional: x=y)
plt.plot([ref.min(), ref.max()], [ref.min(), ref.max()],
         "--", label="Ideal (ref = device)")

plt.xlabel("Device Reading (°C)")
plt.ylabel("Reference Temperature (°C)")
plt.title("Calibration Fit: Reference vs Device Reading")
plt.grid(True, linestyle="--", alpha=0.4)
plt.legend()
plt.tight_layout()
plt.show()
