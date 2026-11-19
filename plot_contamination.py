#!/usr/bin/env python3
"""Draw a picture that explains contamination, using the real scores of the final model.

  python3 plot_contamination.py

Trains the final model again (215 normal training rows, contamination 0.03, seed 0) and shows:
  1. the training set scores and the alarm line the model sets at the weirdest 3%
  2. where the line would sit at 1% and 20%
  3. the test set: how many normal and attack snapshots land past the line
Writes 2.5_hour_run_results/3_model_output/contamination_explained.png. Changes no results.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import evaluate as ev
df = ev.load("2.5_hour_run_results/2_model_input/dataset.csv"); feats = ev.FEATURE_SETS["B_extended"]
fit = df[((df.split == "train") | (df.run == "normal_wave_full")) & (df.label == 0)]
m = ev.fit_iforest(fit[feats], 0.03, 0)
s = -m.score_samples(fit[feats]); line = -m[-1].offset_
test = df[df.split == "test"].reset_index(drop=True); keep = ev.scored_mask(test, 60)
ts = -m.score_samples(test[feats])[keep]; lab = test.label.to_numpy()[keep]
tn, ta = ts[lab == 0], ts[lab == 1]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d3"; BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#9b9a96"
rng = np.random.default_rng(0)
fig, ax = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
def base(a, title, sub):
    a.set_title(title, loc="left", fontsize=12, color=INK, pad=20, fontweight="bold")
    a.text(0, 1.02, sub, transform=a.transAxes, fontsize=9.5, color=MUTED, va="bottom")
    a.set_yticks([]); a.set_ylabel("dots spread up/down\nonly so they don't overlap\n(no meaning)", fontsize=8, color=MUTED, rotation=0, ha="right", va="center", labelpad=8)
    for sd in ("top", "right", "left"): a.spines[sd].set_visible(False)
    a.spines["bottom"].set_color(MUTED); a.tick_params(colors=MUTED, labelsize=9, labelbottom=True)
    a.grid(axis="x", color=GRID, lw=0.6); a.set_axisbelow(True); a.set_ylim(-0.85, 0.85)
def jitter(n, lo=-0.4, hi=0.4): return rng.uniform(lo, hi, n)
# 1 training set
a = ax[0]; base(a, "1. Training set: the model scores the 215 normal snapshots it learned from",
                "Each dot = one 15-second snapshot (one row of data). All of them are normal traffic.")
y = jitter(len(s)); past = s > line
a.scatter(s[~past], y[~past], s=22, color=GRAY, edgecolor="white", lw=0.6, label="normal snapshot")
a.scatter(s[past], y[past], s=36, color=BLUE, edgecolor="white", lw=0.6, label=f"weirdest 3% = {past.sum()} snapshots")
a.axvline(line, color=INK, ls="--", lw=1.3); a.text(line + 0.004, 0.68, f"alarm line = {line:.2f}", fontsize=9.5, color=INK)
a.legend(loc="lower right", fontsize=8.5, frameon=False)
# 2 moving the line
a = ax[1]; base(a, "2. Contamination decides where the alarm line goes",
                "Same 215 training snapshots. We tried several settings on the validation set; 3% scored best.")
a.scatter(s, jitter(len(s)), s=22, color=GRAY, edgecolor="white", lw=0.6)
for c, lab_, col, yy in [(0.20, "20%: too loose,\nmany false alarms", MUTED, -0.78), (0.03, "3%: what we use", INK, 0.68), (0.01, "1%: too strict,\nmisses attacks", MUTED, 0.5)]:
    x = np.percentile(s, 100 - 100 * c); a.axvline(x, color=col, ls="--", lw=1.3 if c == 0.03 else 1)
    a.text(x + 0.004, yy, lab_, fontsize=9, color=col)
# 3 test set
a = ax[2]; base(a, "3. Test set: new snapshots the model never saw. Past the line = ATTACK",
                f"Grey = normal traffic: {(tn > line).sum()} of {len(tn)} past the line = false alarms.   "
                f"Orange = attack traffic: {(ta > line).sum()} of {len(ta)} past the line = caught.")
a.scatter(tn, jitter(len(tn), 0.12, 0.6), s=22, color=GRAY, edgecolor="white", lw=0.6)
a.scatter(ta, jitter(len(ta), -0.6, -0.12), s=26, color=ORANGE, edgecolor="white", lw=0.6)
a.axvline(line, color=INK, ls="--", lw=1.3)
a.text(line + 0.004, 0.68, "ATTACK side →", fontsize=9.5, color=INK); a.text(line - 0.004, 0.68, "← normal side", fontsize=9.5, color=INK, ha="right")
a.text(0.375, -0.82, "orange dots on the left = yo-yo quiet gaps:\nlabelled attack, but the traffic was normal", fontsize=8, color=MUTED)
a.set_xlabel("Weirdness score from Isolation Forest  (left = normal, right = weird)\n"
             "Higher score = the snapshot was cut off from the others in fewer random cuts", fontsize=10, color=INK, labelpad=8)
fig.tight_layout(h_pad=2.2)
out = os.path.join("2.5_hour_run_results", "3_model_output", "contamination_explained.png"); fig.savefig(out, dpi=150); print(out)
