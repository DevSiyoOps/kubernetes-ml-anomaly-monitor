# Checkpoint demo: load the exported dataset and plot it.
# Run as a script:   python3 load_and_plot.py data/run1.csv
# Or paste each "# %%" block into its own notebook cell.

# %% Load the dataset
import sys

import matplotlib.pyplot as plt
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "data/run1.csv"
df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")

print("Shape:", df.shape)
print(df.head())
df.info()

# %% Quick data-quality check
print(df.describe())
print("\nMissing values per column:")
print(df.isna().sum())

# %% Plot replicas, CPU and network traffic over time
# CPU is shown as a % of the pod CPU request. The demo manifest requests 200m (0.2 cores).
fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 8))

df[["current_replicas", "desired_replicas"]].plot(ax=axes[0], drawstyle="steps-post")
axes[0].set_ylabel("replicas")
axes[0].set_title("HPA replicas, CPU and network over time")

(df["cpu_cores"] / 0.2 * 100).plot(ax=axes[1], color="tab:orange")
axes[1].axhline(50, linestyle="--", color="gray", label="HPA target (50%)")
axes[1].set_ylabel("CPU % of request")
axes[1].legend()

df["net_rx_bytes_per_s"].plot(ax=axes[2], color="tab:green")
axes[2].set_ylabel("network rx (B/s)")
axes[2].set_xlabel("time")

plt.tight_layout()
plt.savefig("replicas_plot.png", dpi=150)
plt.show()
