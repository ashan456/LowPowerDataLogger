import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# === Load CSV ===
df = pd.read_csv("hume_data.csv", encoding="cp1252")

# Clean column names
df.columns = [c.strip() for c in df.columns]

# Expecting columns: Reference, Device
print("=== Loaded Data ===")
print(df, "\n")

# Extract the two points
D1, D2 = df["Device"].iloc[0], df["Device"].iloc[1]
R1, R2 = df["Reference"].iloc[0], df["Reference"].iloc[1]

# === 2-point calibration line ===
m = (R2 - R1) / (D2 - D1)
b = R1 - m * D1

print("=== 2-Point Humidity Calibration ===")
print(f"Reference = {m:.6f} * Device + {b:.6f}")
print("\nNote: Only 2 calibration points → regression statistics not valid.\n")

# === Generate points for plotting ===
x_min = min(D1, D2) - 5
x_max = max(D1, D2) + 5

x_fit = np.linspace(x_min, x_max, 200)
y_fit = m * x_fit + b

# === Ideal line (Reference = Device) ===
y_ideal = x_fit

# === Plot ===
plt.figure(figsize=(8, 6))

# Plot calibration points
plt.scatter(df["Device"], df["Reference"], s=90, color="blue", label="Calibration Points")

# Plot calibration line
plt.plot(x_fit, y_fit, 'r-', linewidth=2,
         label=f"Calibration Line")

# # Ideal line
# plt.plot(x_fit, y_ideal, '--', color="gray", label="Ideal (Ref = Device)")

plt.xlabel("Device Reading (%RH)")
plt.ylabel("Reference Humidity (%RH)")
plt.title("2-Point Humidity Calibration Curve")
plt.grid(True, linestyle="--", alpha=0.4)
plt.legend()
plt.tight_layout()
plt.show()
