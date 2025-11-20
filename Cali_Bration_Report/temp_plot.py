import pandas as pd
import matplotlib.pyplot as plt

# === Load CSV with correct encoding ===
df = pd.read_csv("temp_cal.csv", encoding="cp1252")

# Extract columns
ref = df["Reference Temperature (°C)"]
dev = df["Device Reading (°C)"]

# === Plot ===
plt.figure(figsize=(8, 6))

plt.scatter(ref, dev, s=70, marker="o", label="Device Reading")

# Ideal line
min_val = min(ref.min(), dev.min())
max_val = max(ref.max(), dev.max())
plt.plot([min_val, max_val], [min_val, max_val], "--", label="Ideal Line (y=x)")

plt.xlabel("Reference Temperature (°C)")
plt.ylabel("Device Reading (°C)")
plt.title("Calibration Plot: Reference vs Device Reading")
plt.grid(True, linestyle="--", alpha=0.4)
plt.legend()
plt.tight_layout()
plt.show()
