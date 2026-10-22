#!/usr/bin/env python3
"""Train, tune and evaluate the Isolation Forest against two baselines, following the course
pipeline: split -> fit preprocessing on train only -> train -> tune on validation -> finalize ->
evaluate once on test.

  python3 evaluate.py                         # steps 1-5 (tuning on validation, test untouched)
  python3 evaluate.py --final                 # steps 6-7, run ONCE when the choices are final
  python3 evaluate.py --data data/dataset_sim.csv --out results_sim    # practice on synthetic data

Steps
  1  load data/dataset.csv, keep rows inside the runs, add the derived features
  2  split by whole run (by scenario, see SPLIT): no run is in two sets
  3  train on normal rows of the train runs only; the scaler is fitted on those rows only
  4  contamination sweep on validation (contamination = share of NORMAL training rows that the
     model is allowed to flag, because the training data has no attacks)
  5  tune the baselines on the same validation runs; feature-set ablation; plots; report
  6  (--final) refit with the chosen settings on train + the normal validation runs
  7  (--final) score the test runs once, write the metrics, save the model

Metrics (per row): precision, recall, F1, false-positive rate, ROC AUC, average precision.
Per attack episode: episode recall (any alarm during the attack) and detection latency (first
alarm minus attack start). Normal rows in the --grace seconds after an attack ends are not scored,
because the autoscaler is still recovering from the attack then. The Isolation Forest is random, so
its numbers are the mean (and std) over 5 seeds.

Outputs in --out (default results/): report.md, selected.json, contamination_sweep.csv/.png,
baselines.csv, ablation.csv, timeline_<run>.png, and after --final: test_metrics.csv,
final_report.md, test_log.csv, and data/isolation_forest_v2.joblib. Both steps also save
<split>_confusion_matrix.png, <split>_roc_curve.png and <split>_pr_curve.png (lecture slides 9-12).
"""
import argparse
import json
import os
import sys
import time

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score, roc_curve  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

SPLIT = {
    "train": ["normal_steady", "normal_ramp"],
    "validation": ["normal_wave", "attack_flood"],
    "test": ["normal_bursts", "attack_heavy", "attack_yoyo"],
}
BASE = ["replicas", "cpu_utilization", "request_rate"]
EXTRA = ["cpu_per_request", "latency_p95_s", "replica_changes_5m", "cpu_std_5m"]
FEATURE_SETS = {
    "A_original": BASE,                                              # the checkpoint model
    "B_extended": BASE + EXTRA,                                      # the model we propose
    "C_plus_desired": BASE + EXTRA + ["desired_replicas"],           # tests leaving desired out
    "D_plus_network": BASE + EXTRA + ["net_rx_kbps", "net_tx_kbps", "memory_mb"],
}
DEFAULT_FEATURES = "B_extended"
CONTAMINATIONS = [0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2]
SEEDS = [0, 1, 2, 3, 4]
Z_WINDOWS = [8, 20, 40]                  # rows (2, 5, 10 minutes at 15 s)
Z_KS = np.round(np.arange(1.5, 8.01, 0.25), 2)
MODEL_PATH = os.path.join("data", "isolation_forest_v2.joblib")

# chart colours (categorical slots 1-4 of the default validated palette)
C_IF, C_CPU, C_Z, C_4 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, MUTED, ATTACK_FILL = "#0b0b0b", "#52514e", "#e4e2dc"


# ---------------------------------------------------------------- step 1: data

def add_features(run):
    run = run.sort_values("timestamp").copy()
    step = float(np.median(np.diff(run["timestamp"]))) if len(run) > 1 else 15.0
    w = max(2, int(round(300 / step)))        # 5 minutes of rows
    for c in ["replicas", "desired_replicas"]:
        run[c] = run[c].ffill().bfill()
    for c in ["cpu_utilization", "memory_mb"]:
        run[c] = run[c].ffill().fillna(0)
    # CPU per request (% of a core per req/s): high for expensive requests. Below 1 req/s the
    # ratio is noise, so the rate is floored at 1.
    run["cpu_per_request"] = run["cpu_utilization"] / run["request_rate"].clip(lower=1.0)
    # how often the replica count changed in the last 5 minutes: oscillation (yo-yo)
    run["replica_changes_5m"] = (run["replicas"].diff().fillna(0) != 0).rolling(w, min_periods=1).sum()
    run["cpu_std_5m"] = run["cpu_utilization"].rolling(w, min_periods=2).std().fillna(0)
    run["step_s"] = step
    return run


def load(path):
    if not os.path.exists(path):
        sys.exit(f"{path} not found. Make it with export_runs.py (or simulate_runs.py for practice).")
    df = pd.read_csv(path)
    if "in_run" in df:
        df = df[df["in_run"] == 1]
    df = pd.concat([add_features(g) for _, g in df.groupby("run")], ignore_index=True)
    where = {s: name for name, scen in SPLIT.items() for s in scen}
    df["split"] = df["scenario"].map(where)
    missing = sorted(set(df.loc[df["split"].isna(), "scenario"]))
    if missing:
        print(f"note: scenarios not in SPLIT are ignored: {', '.join(missing)}")
        df = df[df["split"].notna()]
    for name in SPLIT:
        if not (df["split"] == name).any():
            sys.exit(f"no runs for the {name} split (needs {', '.join(SPLIT[name])})")
    df = df.fillna({c: 0 for c in ["net_rx_kbps", "net_tx_kbps", "latency_p95_s"] if c in df})
    return df.reset_index(drop=True)


def scored_mask(df, grace):
    """False for normal rows in the grace period after an attack ends (same run)."""
    keep = np.ones(len(df), dtype=bool)
    att = df[df["label"] == 1]
    for (run, _), ev in att.groupby(["run", "event_id"]):
        end = ev["timestamp"].max() + ev["step_s"].iloc[0]
        m = ((df["run"] == run) & (df["label"] == 0) & (df["timestamp"] >= end)
             & (df["timestamp"] < end + grace)).to_numpy()
        keep[m] = False
    return keep


# ---------------------------------------------------------------- metrics

def evaluate_flags(df, flag, score, keep):
    y = df["label"].to_numpy()[keep]
    f = np.asarray(flag, dtype=bool)[keep]
    s = np.asarray(score, dtype=float)[keep]
    tp, fp = int((f & (y == 1)).sum()), int((f & (y == 0)).sum())
    fn, tn = int((~f & (y == 1)).sum()), int((~f & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else float("nan")
    out = dict(precision=prec, recall=rec,
               f1=2 * prec * rec / (prec + rec) if prec + rec else 0.0,
               fpr=fp / (fp + tn) if fp + tn else float("nan"), tp=tp, fp=fp, fn=fn, tn=tn)
    both = len(set(y)) == 2
    out["roc_auc"] = roc_auc_score(y, s) if both else float("nan")
    out["avg_precision"] = average_precision_score(y, s) if both else float("nan")

    lat, detected, n_ep = [], 0, 0
    flag_all = np.asarray(flag, dtype=bool)
    for _, ev in df[df["label"] == 1].groupby(["run", "event_id"]):
        n_ep += 1
        hits = ev["timestamp"].to_numpy()[flag_all[ev.index.to_numpy()]]
        if len(hits):
            detected += 1
            lat.append(hits.min() - ev["event_start"].iloc[0])
    out.update(episodes=n_ep, episodes_detected=detected,
               episode_recall=detected / n_ep if n_ep else float("nan"),
               latency_mean_s=float(np.mean(lat)) if lat else float("nan"),
               latency_max_s=float(np.max(lat)) if lat else float("nan"))
    return out


def mean_std(rows):
    d = pd.DataFrame(rows)
    num = d.select_dtypes("number")
    return num.mean().to_dict(), num.std(ddof=0).to_dict()


# ---------------------------------------------------------------- detectors

def fit_iforest(X, contamination, seed):
    model = make_pipeline(StandardScaler(),
                          IsolationForest(n_estimators=200, contamination=contamination, random_state=seed))
    return model.fit(X)


def iforest_outputs(model, X):
    return model.predict(X) == -1, -model.score_samples(X)


def zscore(df, window):
    """Largest upward z-score of CPU and request rate vs the previous `window` rows of the same run."""
    out = np.zeros(len(df))
    for _, g in df.groupby("run"):
        zs = []
        for c in ["cpu_utilization", "request_rate"]:
            x = g[c]
            prev = x.shift(1).rolling(window, min_periods=4)
            sd = np.maximum(prev.std(), 0.05 * prev.mean().abs() + 0.5)  # floor: steady signals
            zs.append(((x - prev.mean()) / sd).fillna(0))
        out[g.index.to_numpy()] = np.maximum(*zs)
    return out


def tune_threshold(df, score, keep, candidates):
    best = None
    for t in candidates:
        m = evaluate_flags(df, score > t, score, keep)
        if best is None or (m["f1"], -m["fpr"]) > (best[1]["f1"], -best[1]["fpr"]):
            best = (float(t), m)
    return best


def tune_baselines(val, keep):
    cpu = val["cpu_utilization"].to_numpy()
    cands = np.unique(np.quantile(cpu, np.linspace(0.3, 0.999, 300)))
    t_cpu, m_cpu = tune_threshold(val, cpu, keep, cands)
    best_z = None
    for w in Z_WINDOWS:
        z = zscore(val, w)
        k, m = tune_threshold(val, z, keep, Z_KS)
        if best_z is None or (m["f1"], -m["fpr"]) > (best_z[2]["f1"], -best_z[2]["fpr"]):
            best_z = (w, k, m)
    return {"cpu_threshold": t_cpu, "z_window": best_z[0], "z_k": best_z[1]}, m_cpu, best_z[2]


def sweep(train_X, val, val_X, keep):
    rows = []
    for c in CONTAMINATIONS:
        per_seed = [evaluate_flags(val, *iforest_outputs(fit_iforest(train_X, c, s), val_X), keep) for s in SEEDS]
        mean, std = mean_std(per_seed)
        rows.append(dict(contamination=c, **mean, **{f"{k}_std": v for k, v in std.items()
                                                       if k in ("precision", "recall", "f1", "fpr")}))
    return pd.DataFrame(rows)


def pick(sw):
    s = sw.assign(neg_fpr=-sw["fpr"], neg_c=-sw["contamination"])
    return float(s.sort_values(["f1", "neg_fpr", "neg_c"], ascending=False).iloc[0]["contamination"])


# ---------------------------------------------------------------- plots

def style(ax):
    ax.grid(True, color="#d9d8d3", linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=8)


def plot_sweep(sw, chosen, path, title):
    fig, ax = plt.subplots(figsize=(8, 4))
    for col, color, name in [("precision", C_IF, "precision"), ("recall", C_CPU, "recall"),
                             ("f1", C_Z, "F1"), ("fpr", C_4, "false-positive rate")]:
        ax.errorbar(sw["contamination"], sw[col], yerr=sw.get(f"{col}_std"), color=color, lw=2,
                    marker="o", ms=5, capsize=2, label=name)
    ax.axvline(chosen, color=MUTED, ls="--", lw=1)
    ax.text(chosen, 0.5, f" chosen {chosen:g}", color=MUTED, fontsize=8)
    ax.set_xscale("log")
    ax.set_xticks(CONTAMINATIONS, [f"{c:g}" for c in CONTAMINATIONS])
    ax.minorticks_off()
    ax.set_xlabel("contamination (share of normal training rows flagged)", color=INK, fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    style(ax)
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1))
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_eval_figs(df, keep, dets, prefix, out_dir, synthetic):
    """Confusion matrices, ROC curve and precision-recall curve (lecture slides 9-12).
    dets: [(name, flags, scores, colour)]. Rows in the post-attack grace period are left out."""
    y = df["label"].to_numpy()[keep]
    tag = "  [SYNTHETIC]" if synthetic else ""
    split = prefix.rstrip("_")

    fig, axes = plt.subplots(1, len(dets), figsize=(3.6 * len(dets), 3.4))
    for ax, (name, flag, _, _) in zip(np.atleast_1d(axes), dets):
        f = np.asarray(flag, dtype=bool)[keep]
        # same layout as the lecture: rows = actual (attack, normal), columns = predicted (attack, normal)
        cells = [[(f & (y == 1)).sum(), (~f & (y == 1)).sum()], [(f & (y == 0)).sum(), (~f & (y == 0)).sum()]]
        names = [["TP", "FN"], ["FP", "TN"]]
        for i in range(2):
            row_total = max(1, cells[i][0] + cells[i][1])
            for j in range(2):
                share = cells[i][j] / row_total
                ax.add_patch(plt.Rectangle((j, 1 - i), 1, 1, facecolor=plt.cm.Blues(0.1 + 0.75 * share),
                                           edgecolor="white", lw=2))
                ax.text(j + 0.5, 1.5 - i, f"{names[i][j]}\n{cells[i][j]}\n{share:.0%}", ha="center", va="center",
                        fontsize=9, color="white" if share > 0.6 else INK)
        ax.set_xlim(0, 2)
        ax.set_ylim(0, 2)
        ax.set_xticks([0.5, 1.5], ["attack", "normal"], fontsize=8)
        ax.set_yticks([1.5, 0.5], ["attack", "normal"], fontsize=8)
        ax.set_xlabel("predicted", color=MUTED, fontsize=8)
        ax.set_ylabel("actual", color=MUTED, fontsize=8)
        ax.set_title(name, color=INK, fontsize=9, loc="left")
        ax.tick_params(length=0, colors=MUTED)
        for side in ax.spines.values():
            side.set_visible(False)
    fig.suptitle(f"Confusion matrices on {split} rows (% of each actual row){tag}", color=INK, fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, f"{prefix}confusion_matrix.png"), dpi=150)
    plt.close(fig)

    if len(set(y)) < 2:
        print(f"  {split}: only one class in the scored rows, ROC and precision-recall curves skipped")
        return
    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    for name, _, score, color in dets:
        sc = np.asarray(score, dtype=float)[keep]
        fpr, tpr, _ = roc_curve(y, sc)
        ax.plot(fpr, tpr, color=color, lw=2, label=f"{name} (AUC {roc_auc_score(y, sc):.2f})")
    ax.plot([0, 1], [0, 1], color=MUTED, ls="--", lw=1, label="random guess")
    ax.set_xlabel("false-positive rate", color=INK, fontsize=9)
    ax.set_ylabel("true-positive rate (recall)", color=INK, fontsize=9)
    ax.set_title(f"ROC curve, {split}{tag}", color=INK, fontsize=10, loc="left")
    style(ax)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, f"{prefix}roc_curve.png"), dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    for name, _, score, color in dets:
        sc = np.asarray(score, dtype=float)[keep]
        prec, rec, _ = precision_recall_curve(y, sc)
        ax.plot(rec, prec, color=color, lw=2, label=f"{name} (AP {average_precision_score(y, sc):.2f})")
    ax.axhline(y.mean(), color=MUTED, ls="--", lw=1, label=f"random guess ({y.mean():.2f})")
    ax.set_xlabel("recall", color=INK, fontsize=9)
    ax.set_ylabel("precision", color=INK, fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_title(f"Precision-recall curve, {split}{tag}", color=INK, fontsize=10, loc="left")
    style(ax)
    ax.legend(frameon=False, fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, f"{prefix}pr_curve.png"), dpi=150)
    plt.close(fig)


def plot_timeline(run, flags, path, synthetic):
    t = (run["timestamp"] - run["timestamp"].min()) / 60
    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(10, 7),
                             gridspec_kw={"height_ratios": [2, 2, 1.5, 1.2]})
    panels = [("cpu_utilization", "CPU (% of a core)"), ("request_rate", "requests / s"),
              ("replicas", "replicas")]
    for ax, (col, name) in zip(axes, panels):
        ax.plot(t, run[col], color=INK, lw=1.5, drawstyle="steps-post" if col == "replicas" else "default")
        ax.set_ylabel(name, color=INK, fontsize=8)
    ax = axes[3]
    for i, (name, f, color) in enumerate(flags):
        f = np.asarray(f, dtype=bool)
        ax.scatter(t[f], np.full(f.sum(), i), marker="|", s=120, color=color, linewidths=2)
    ax.set_yticks(range(len(flags)), [n for n, _, _ in flags], fontsize=8)
    ax.set_ylim(-0.6, len(flags) - 0.4)
    ax.set_xlabel("minutes since run start", color=INK, fontsize=9)
    att = run["label"].to_numpy() == 1
    step = run["step_s"].iloc[0] / 60
    for a in axes:
        style(a)
        for _, ev in run[att].groupby("event_id"):
            a.axvspan(((ev["event_start"].iloc[0] - run["timestamp"].min()) / 60),
                      t[ev.index].max() + step, color=ATTACK_FILL, zorder=0, lw=0)
    title = f"{run['run'].iloc[0]}: shaded = labelled attack, bottom = alarms"
    if synthetic:
        title += "   [SYNTHETIC practice data]"
    axes[0].set_title(title, color=INK, fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- report helpers

def fmt(v, pct=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    return f"{v:.3f}" if not pct else f"{v:.1%}"


def table(rows, cols):
    head = "| " + " | ".join(c for c, _ in cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(f(r) for _, f in cols) + " |\n" for r in rows)


METRIC_COLS = [
    ("precision", lambda r: fmt(r["precision"])), ("recall", lambda r: fmt(r["recall"])),
    ("F1", lambda r: fmt(r["f1"])), ("FPR", lambda r: fmt(r["fpr"])),
    ("ROC AUC", lambda r: fmt(r["roc_auc"])), ("avg precision", lambda r: fmt(r["avg_precision"])),
    ("episodes found", lambda r: f"{r['episodes_detected']:.1f}/{r['episodes']:.0f}"),
    ("mean latency s", lambda r: "n/a" if np.isnan(r["latency_mean_s"]) else f"{r['latency_mean_s']:.0f}"),
]


def per_run(df, keep, detectors):
    """One row per (run, detector). detectors: [(name, [(flags, scores), ...])], several pairs = seeds."""
    rows = []
    for run_name in sorted(df["run"].unique()):
        m = (df["run"] == run_name).to_numpy()
        sub = df[m].reset_index(drop=True)
        for name, outs in detectors:
            res = [evaluate_flags(sub, np.asarray(f)[m], np.asarray(sc)[m], keep[m]) for f, sc in outs]
            rows.append(dict(run=run_name, detector=name, **mean_std(res)[0]))
    return rows


RUN_COLS = [
    ("run", lambda r: r["run"]), ("detector", lambda r: r["detector"]),
    ("FPR", lambda r: fmt(r["fpr"])), ("recall", lambda r: fmt(r["recall"])),
    ("episodes found", lambda r: f"{r['episodes_detected']:.1f}/{r['episodes']:.0f}" if r["episodes"] else "-"),
    ("mean latency s", lambda r: "n/a" if np.isnan(r["latency_mean_s"]) else f"{r['latency_mean_s']:.0f}"),
]


def data_summary(df):
    g = df.groupby(["split", "run"]).agg(rows=("label", "size"), attack_rows=("label", "sum")).reset_index()
    order = {k: i for i, k in enumerate(SPLIT)}
    g = g.sort_values(["split", "run"], key=lambda s: s.map(order) if s.name == "split" else s)
    return table(g.to_dict("records"), [("split", lambda r: r["split"]), ("run", lambda r: r["run"]),
                                        ("rows", lambda r: str(r["rows"])),
                                        ("attack rows", lambda r: str(r["attack_rows"]))])


def is_synthetic(df):
    return "synthetic" in df and bool(df["synthetic"].fillna(0).astype(int).any())


# ---------------------------------------------------------------- steps 1-5

def tuning(args):
    df = load(args.data)
    synthetic = is_synthetic(df)
    tr = df[(df["split"] == "train") & (df["label"] == 0)]
    val = df[df["split"] == "validation"].reset_index(drop=True)
    keep = scored_mask(val, args.grace)
    feats = FEATURE_SETS[args.features]
    print(f"rows: train {len(tr)} (normal only), validation {len(val)} "
          f"({int(val['label'].sum())} attack), test {(df['split'] == 'test').sum()} (not used here)")
    if (df.loc[df["split"] == "train", "label"] == 1).any():
        print("note: attack rows in the train runs were dropped; train is normal-only")

    print(f"step 4: contamination sweep with {args.features} ({len(SEEDS)} seeds each)")
    sw = sweep(tr[feats], val, val[feats], keep)
    chosen = pick(sw)
    sw.to_csv(os.path.join(args.out, "contamination_sweep.csv"), index=False)
    plot_sweep(sw, chosen, os.path.join(args.out, "contamination_sweep.png"),
               f"Isolation Forest on validation ({args.features}), mean of {len(SEEDS)} seeds"
               + ("  [SYNTHETIC]" if synthetic else ""))

    print("step 5: baselines and feature ablation")
    params, m_cpu, m_z = tune_baselines(val, keep)
    if_val = sw[sw["contamination"] == chosen].iloc[0].to_dict()
    base_rows = [dict(detector=f"Isolation Forest ({args.features}, c={chosen:g})", **if_val),
                 dict(detector=f"CPU threshold (> {params['cpu_threshold']:.1f}%)", **m_cpu),
                 dict(detector=f"rolling z-score (window {params['z_window']}, k={params['z_k']})", **m_z)]
    pd.DataFrame(base_rows).to_csv(os.path.join(args.out, "baselines.csv"), index=False)

    abl = []
    for name, cols in FEATURE_SETS.items():
        missing = [c for c in cols if c not in df]
        if missing:
            print(f"  ablation {name}: skipped, missing {missing}")
            continue
        s = sw if name == args.features else sweep(tr[cols], val, val[cols], keep)
        c = pick(s)
        abl.append(dict(feature_set=name, features=", ".join(cols), contamination=c,
                        **s[s["contamination"] == c].iloc[0].drop("contamination").to_dict()))
    pd.DataFrame(abl).to_csv(os.path.join(args.out, "ablation.csv"), index=False)

    if_outs = [iforest_outputs(fit_iforest(tr[feats], chosen, s), val[feats]) for s in SEEDS]
    f_if = if_outs[0][0]
    z = zscore(val, params["z_window"])
    cpu_v = val["cpu_utilization"].to_numpy()
    run_rows = per_run(val, keep, [("Isolation Forest", if_outs), ("CPU threshold", [(cpu_v > params["cpu_threshold"], cpu_v)]),
                                   ("z-score", [(z > params["z_k"], z)])])
    pd.DataFrame(run_rows).to_csv(os.path.join(args.out, "per_run_validation.csv"), index=False)
    plot_eval_figs(val, keep, [("Isolation Forest", f_if, if_outs[0][1], C_IF),
                               ("CPU threshold", cpu_v > params["cpu_threshold"], cpu_v, C_CPU),
                               ("z-score", z > params["z_k"], z, C_Z)], "validation_", args.out, synthetic)
    for run_name, run in val.groupby("run"):
        idx = run.index.to_numpy()
        plot_timeline(run, [("Isolation Forest", f_if[idx], C_IF),
                            ("CPU threshold", val["cpu_utilization"].to_numpy()[idx] > params["cpu_threshold"], C_CPU),
                            ("z-score", z[idx] > params["z_k"], C_Z)],
                      os.path.join(args.out, f"timeline_{run_name}.png"), synthetic)

    selected = dict(features=args.features, feature_list=feats, contamination=chosen, grace_s=args.grace,
                    seeds=SEEDS, data=args.data, synthetic=synthetic, **params,
                    chosen_on=time.strftime("%Y-%m-%d %H:%M"))
    with open(os.path.join(args.out, "selected.json"), "w") as f:
        json.dump(selected, f, indent=2)

    rep = ["# Validation report (steps 1-5)\n"]
    if synthetic:
        rep.append("> **SYNTHETIC practice data from simulate_runs.py. These numbers are not results.**\n")
    rep += [f"Data: `{args.data}`, rows inside runs only, one row per {df['step_s'].median():.0f} s. "
            f"Normal rows in the {args.grace:.0f} s after an attack are not scored.\n",
            "## Split (by whole run)\n", data_summary(df),
            f"\nTraining uses only the normal rows of the train runs ({len(tr)} rows). The scaler and the "
            "model are fitted on those rows only. The test runs are not used in this report.\n",
            f"## Contamination sweep ({args.features}, validation, mean of {len(SEEDS)} seeds)\n",
            "With normal-only training, contamination is the share of normal training rows the model "
            "may call anomalous, so it sets the alarm threshold. It was chosen by the best mean F1 on "
            "validation (ties: lower false-positive rate).\n",
            table(sw.to_dict("records"), [("contamination", lambda r: f"{r['contamination']:g}")] + METRIC_COLS),
            f"\nChosen: **{chosen:g}**. Plot: `contamination_sweep.png`.\n",
            "Confusion matrices, ROC and precision-recall curves on validation (Isolation Forest seed 0): "
            "`validation_confusion_matrix.png`, `validation_roc_curve.png`, `validation_pr_curve.png`.\n",
            "## Detectors on validation (each tuned on these runs)\n",
            table(base_rows, [("detector", lambda r: r["detector"])] + METRIC_COLS),
            "\n## Per workload (each validation run)\n",
            "FPR on a normal run = share of its rows flagged by mistake. Recall and episodes only apply to attack runs.\n",
            table(run_rows, RUN_COLS),
            "\n## Feature ablation (validation, each set with its own best contamination)\n",
            table(abl, [("set", lambda r: r["feature_set"]), ("c", lambda r: f"{r['contamination']:g}")] + METRIC_COLS),
            "\nSets: " + "; ".join(f"**{k}** = {', '.join(v)}" for k, v in FEATURE_SETS.items()) + ".\n",
            "\n`desired_replicas` (set C) is the HPA's own reaction to CPU, so it repeats a signal the "
            "model already has and changes whenever the HPA settings change. Compare C with B: if C is "
            "not clearly better, leaving it out costs nothing and keeps the model independent of the "
            "HPA configuration.\n",
            "\nNext: when these choices are final, run `python3 evaluate.py --final` once.\n"]
    with open(os.path.join(args.out, "report.md"), "w") as f:
        f.write("\n".join(rep))
    print(f"chosen contamination {chosen:g}; validation F1 IF {if_val['f1']:.3f}, "
          f"CPU threshold {m_cpu['f1']:.3f}, z-score {m_z['f1']:.3f}")
    print(f"wrote {args.out}/report.md (and csv/png files)")


# ---------------------------------------------------------------- steps 6-7

def final(args):
    sel_path = os.path.join(args.out, "selected.json")
    if not os.path.exists(sel_path):
        sys.exit(f"{sel_path} not found: run python3 evaluate.py (steps 1-5) first.")
    with open(sel_path) as f:
        sel = json.load(f)
    log_path = os.path.join(args.out, "test_log.csv")
    if os.path.exists(log_path):
        prev = pd.read_csv(log_path)
        print(f"WARNING: the test set has already been evaluated {len(prev)} time(s) "
              f"(see {log_path}). Re-running after changing settings is tuning on the test set.")
    df = load(args.data)
    synthetic = is_synthetic(df)
    feats = sel["feature_list"]
    val = df[df["split"] == "validation"]
    normal_val_runs = val.groupby("run")["label"].max().pipe(lambda s: s[s == 0].index)
    fit_rows = df[((df["split"] == "train") | df["run"].isin(normal_val_runs)) & (df["label"] == 0)]
    test = df[df["split"] == "test"].reset_index(drop=True)
    keep = scored_mask(test, sel["grace_s"])
    print(f"step 6: refit on {len(fit_rows)} normal rows (train + {', '.join(normal_val_runs)}), "
          f"{sel['features']}, contamination {sel['contamination']:g}")

    per_seed, models = [], []
    for s in sel["seeds"]:
        m = fit_iforest(fit_rows[feats], sel["contamination"], s)
        models.append(m)
        per_seed.append(evaluate_flags(test, *iforest_outputs(m, test[feats]), keep))
    if_mean, if_std = mean_std(per_seed)
    cpu = test["cpu_utilization"].to_numpy()
    z = zscore(test, sel["z_window"])
    m_cpu = evaluate_flags(test, cpu > sel["cpu_threshold"], cpu, keep)
    m_z = evaluate_flags(test, z > sel["z_k"], z, keep)
    rows = [dict(detector="Isolation Forest", **if_mean), dict(detector="CPU threshold", **m_cpu),
            dict(detector="rolling z-score", **m_z)]
    pd.DataFrame(rows).to_csv(os.path.join(args.out, "test_metrics.csv"), index=False)

    by_kind = []
    for kind in sorted(set(test.loc[test["label"] == 1, "kind"])):
        sub_idx = ((test["label"] == 0) | (test["kind"] == kind)).to_numpy()
        sub = test[sub_idx].reset_index(drop=True)
        ks = keep[sub_idx]
        res = [evaluate_flags(sub, *iforest_outputs(mm, sub[feats]), ks) for mm in models]
        by_kind.append(dict(detector=f"Isolation Forest, {kind} only", **mean_std(res)[0]))
        by_kind.append(dict(detector=f"CPU threshold, {kind} only",
                            **evaluate_flags(sub, sub["cpu_utilization"] > sel["cpu_threshold"],
                                             sub["cpu_utilization"], ks)))
        zz = z[sub_idx]
        by_kind.append(dict(detector=f"z-score, {kind} only", **evaluate_flags(sub, zz > sel["z_k"], zz, ks)))

    if_outs = [iforest_outputs(mm, test[feats]) for mm in models]
    f_if = if_outs[0][0]
    run_rows = per_run(test, keep, [("Isolation Forest", if_outs), ("CPU threshold", [(cpu > sel["cpu_threshold"], cpu)]),
                                    ("z-score", [(z > sel["z_k"], z)])])
    pd.DataFrame(run_rows).to_csv(os.path.join(args.out, "per_run_test.csv"), index=False)
    plot_eval_figs(test, keep, [("Isolation Forest", f_if, if_outs[0][1], C_IF),
                                ("CPU threshold", cpu > sel["cpu_threshold"], cpu, C_CPU),
                                ("z-score", z > sel["z_k"], z, C_Z)], "test_", args.out, synthetic)
    for run_name, run in test.groupby("run"):
        idx = run.index.to_numpy()
        plot_timeline(run, [("Isolation Forest", f_if[idx], C_IF), ("CPU threshold", cpu[idx] > sel["cpu_threshold"], C_CPU),
                            ("z-score", z[idx] > sel["z_k"], C_Z)],
                      os.path.join(args.out, f"test_timeline_{run_name}.png"), synthetic)

    if not synthetic:
        joblib.dump({"model": models[0], "features": feats, "contamination": sel["contamination"],
                     "trained_on": sorted(fit_rows["run"].unique()), "saved": time.strftime("%Y-%m-%d %H:%M")},
                    MODEL_PATH)
        print(f"step 7: model saved to {MODEL_PATH}")
    else:
        print("step 7: synthetic data, model not saved")

    std_txt = ", ".join(f"{k} ±{if_std[k]:.3f}" for k in ("precision", "recall", "f1", "fpr"))
    rep = ["# Test results (steps 6-7, evaluated once)\n"]
    if synthetic:
        rep.append("> **SYNTHETIC practice data from simulate_runs.py. These numbers are not results.**\n")
    rep += [f"Settings fixed on validation ({sel['chosen_on']}): features {sel['features']} "
            f"({', '.join(feats)}), contamination {sel['contamination']:g}, CPU threshold "
            f"{sel['cpu_threshold']:.1f}%, z-score window {sel['z_window']} rows, k={sel['z_k']}, "
            f"grace {sel['grace_s']:.0f} s.\n",
            f"Isolation Forest refitted on {len(fit_rows)} normal rows: {', '.join(sorted(fit_rows['run'].unique()))}.\n",
            "## Test split\n", data_summary(df[df["split"] == "test"]),
            "\n## All test runs\n", table(rows, [("detector", lambda r: r["detector"])] + METRIC_COLS),
            f"\nIsolation Forest: mean of {len(models)} seeds (std: {std_txt}).\n",
            "Confusion matrices, ROC and precision-recall curves (Isolation Forest seed 0): "
            "`test_confusion_matrix.png`, `test_roc_curve.png`, `test_pr_curve.png`.\n",
            "\n## Per workload (each test run)\n",
            "FPR on a normal run = share of its rows flagged by mistake. Recall and episodes only apply to attack runs.\n",
            table(run_rows, RUN_COLS),
            "\n## Per attack type (normal test rows + that attack's rows)\n",
            table(by_kind, [("detector", lambda r: r["detector"])] + METRIC_COLS),
            "\nThe yo-yo episode is labelled as one window including its quiet halves, so its row "
            "recall understates detection; episode recall is the fairer figure. Episode counts are small.\n"]
    with open(os.path.join(args.out, "final_report.md"), "w") as f:
        f.write("\n".join(rep))
    log = pd.DataFrame([dict(when=time.strftime("%Y-%m-%d %H:%M:%S"), data=args.data, features=sel["features"],
                             contamination=sel["contamination"], f1_iforest=if_mean["f1"],
                             f1_cpu=m_cpu["f1"], f1_z=m_z["f1"])])
    log.to_csv(log_path, mode="a", header=not os.path.exists(log_path), index=False)
    for r in rows:
        print(f"  {r['detector']:<18} precision {r['precision']:.3f}  recall {r['recall']:.3f}  "
              f"F1 {r['f1']:.3f}  FPR {r['fpr']:.3f}  episodes {r['episodes_detected']:.1f}/{r['episodes']:.0f}")
    print(f"wrote {args.out}/final_report.md")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=os.path.join("data", "dataset.csv"))
    p.add_argument("--out", default="results")
    p.add_argument("--features", default=DEFAULT_FEATURES, choices=list(FEATURE_SETS))
    p.add_argument("--grace", type=float, default=60, help="seconds after an attack not scored as false alarms")
    p.add_argument("--final", action="store_true", help="steps 6-7: refit and evaluate on test ONCE")
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)
    final(args) if args.final else tuning(args)


if __name__ == "__main__":
    main()
