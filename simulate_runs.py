#!/usr/bin/env python3
"""SYNTHETIC practice data in the same format as export_runs.py, for testing evaluate.py before the
real runs exist. Every file it writes is marked synthetic (column synthetic=1, meta "synthetic": true)
and goes to a separate folder, so it cannot be mixed up with real data.

  python3 simulate_runs.py                    # one run of each scenario
  python3 simulate_runs.py --repeats 2        # two of each (suffixes _r1, _r2)
  python3 evaluate.py --data data/dataset_sim.csv --out results_sim

Writes data/runs_sim/<run>/{labels.csv, meta.json, metrics.csv} and data/dataset_sim.csv.

The model is deliberately simple and is NOT a result: each load segment offers
workers / (ms/1000 + sleep) requests per second, each using ms of CPU; a pod can use at most one
core (single Flask process); when demand exceeds capacity the extra requests queue and latency grows.
An HPA with target 50% of a 200m request, 1 to 10 replicas, 30 s pod start-up and a 120 s scale-down
window reacts every 15 s. Metrics are smoothed over 1 minute (like rate(...[1m])) and get noise.
"""
import argparse
import json
import math
import os

import numpy as np
import pandas as pd

from export_runs import PRE_ROLL_S, label_rows
from run_scenarios import REQ_OVERHEAD_S, SCENARIOS, build_scenario, write_labels

STEP = 15
CPU_REQUEST = 0.2       # cores per pod (HPA target is a share of this)
TARGET = 0.5
MIN_R, MAX_R = 1, 10
POD_CORES = 1.0         # one Flask process cannot use more than one core
NODE_CORES = 3.0        # what is left of a laptop's kind node for the app
START_DELAY_S = 30
DOWN_WINDOW_S = 120
IDLE_CORES = 0.003      # per pod, metrics scrapes and Python idle


def offered(segs, t):
    """(req/s, cpu cores demanded, list of (rps, ms)) at second t."""
    mix = []
    for s in segs:
        if s["start"] <= t < s["end"] and s["workers"] > 0:
            mix.append((s["workers"] / (s["ms"] / 1000 + s["sleep"] + REQ_OVERHEAD_S), s["ms"]))
    rps = sum(r for r, _ in mix)
    cores = sum(r * ms / 1000 for r, ms in mix) + rps * 0.0005   # 0.5 ms of Flask overhead per request
    return rps, cores, mix


def p95_ms(mix):
    total = sum(r for r, _ in mix)
    if total == 0:
        return 0.0
    acc = 0.0
    for r, ms in sorted(mix, key=lambda x: -x[1]):
        acc += r
        if acc >= 0.05 * total:
            return ms
    return mix[-1][1]


def simulate(segs, duration, rng):
    t_grid = np.arange(-PRE_ROLL_S, duration + 1, 1)   # 1 s resolution, sampled every STEP later
    ready, desired = MIN_R, MIN_R
    pending = []              # (time ready, new count)
    history = []              # (t, desired) for the scale-down window
    rows = []
    for t in t_grid:
        for tr, n in list(pending):   # new pods become ready START_DELAY_S after the decision
            if tr <= t:
                ready = n
                pending.remove((tr, n))
        rps, cores, mix = offered(segs, t)
        cap = min(ready * POD_CORES, NODE_CORES)
        load = cores / cap if cap else 0
        served = min(1.0, 1 / load) if load > 0 else 1.0
        used = min(cores, cap) + ready * IDLE_CORES
        slow = 1 + 4 * max(0.0, load - 0.8) + (load > 1) * 3 * (load - 1)
        lat = p95_ms(mix) / 1000 * slow + (0.004 if rps else 0)
        rows.append(dict(t=t, replicas=ready, desired_replicas=desired, cores=used, rps=rps * served,
                         lat=lat if rps else 0.0, inflight=rps * served * lat))
        if t % 15 == 0:   # HPA sync
            util = used / (ready * CPU_REQUEST)
            want = ready if abs(util / TARGET - 1) <= 0.1 else math.ceil(ready * util / TARGET)
            want = int(np.clip(want, MIN_R, MAX_R))
            history.append((t, want))
            history = [(ht, w) for ht, w in history if ht > t - DOWN_WINDOW_S]
            target_n = want if want > ready else max(w for _, w in history)  # scale down slowly
            if target_n != desired:
                desired = target_n
                if desired > ready:
                    pending.append((t + START_DELAY_S, desired))
                else:
                    pending = []
                    ready = desired
    df = pd.DataFrame(rows)
    smooth = df[["cores", "rps", "lat", "inflight"]].rolling(60, min_periods=1).mean()
    df[["cores", "rps", "lat", "inflight"]] = smooth
    df = df[df["t"] % STEP == 0].reset_index(drop=True)
    n = len(df)
    noise = lambda sd: rng.normal(1, sd, n).clip(0.5, 1.5)  # noqa: E731
    out = pd.DataFrame({
        "replicas": df["replicas"].astype(float),
        "desired_replicas": df["desired_replicas"].astype(float),
        "cpu_utilization": df["cores"] * 100 * noise(0.08) + rng.normal(0, 0.05, n).clip(0),
        "request_rate": df["rps"] * noise(0.05),
        "latency_p95_s": df["lat"] * noise(0.1),
        "inflight": np.round(df["inflight"] * noise(0.2)),
        "memory_mb": df["replicas"] * 42 + df["rps"] * 0.2 + rng.normal(0, 1.5, n),
        "net_rx_kbps": df["rps"] * 0.21 * noise(0.05) + df["replicas"] * 0.05,
        "net_tx_kbps": df["rps"] * 0.19 * noise(0.05) + df["replicas"] * 0.4,
    })
    return df["t"].to_numpy(), out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--runs-dir", default=os.path.join("data", "runs_sim"))
    p.add_argument("--out", default=os.path.join("data", "dataset_sim.csv"))
    args = p.parse_args()

    frames = []
    t_base = 1_800_000_000   # fake clock; runs are placed one after another, an hour apart
    for rep in range(args.repeats):
        for k, scenario in enumerate(SCENARIOS):
            run = scenario + (f"_r{rep + 1}" if args.repeats > 1 else "")
            rng = np.random.default_rng(args.seed * 1000 + rep * 100 + k)
            duration, segs = build_scenario(scenario)
            t0 = t_base + (rep * len(SCENARIOS) + k) * 3600
            rel_t, df = simulate(segs, duration, rng)
            df.insert(0, "timestamp", (t0 + rel_t).astype(float))
            labels = pd.DataFrame([dict(s, start=t0 + s["start"], end=t0 + s["end"]) for s in segs])
            df = label_rows(df, labels, t0, t0 + duration)
            df.insert(df.columns.get_loc("phase"), "scenario", scenario)
            df.insert(df.columns.get_loc("scenario"), "run", run)
            df["synthetic"] = 1

            run_dir = os.path.join(args.runs_dir, run)
            os.makedirs(run_dir, exist_ok=True)
            write_labels(os.path.join(run_dir, "labels.csv"), segs, t0)
            meta = {"run": run, "scenario": scenario, "description": SCENARIOS[scenario][0],
                    "start": t0, "end": t0 + duration, "duration_s": duration, "scale": 1.0,
                    "synthetic": True, "complete": True,
                    "note": "SYNTHETIC practice data from simulate_runs.py, not a measurement",
                    "hpa": {"minReplicas": MIN_R, "maxReplicas": MAX_R, "cpu_target": TARGET,
                            "scale_down_window_s": DOWN_WINDOW_S}}
            with open(os.path.join(run_dir, "meta.json"), "w") as f:
                json.dump(meta, f, indent=2)
            df.to_csv(os.path.join(run_dir, "metrics.csv"), index=False)
            r = df[df["in_run"] == 1]
            print(f"  {run:<22} {len(r):>4} rows  attack rows {r['label'].sum():>3}  "
                  f"max replicas {r['replicas'].max():.0f}  max cpu {r['cpu_utilization'].max():.0f}%  "
                  f"max req/s {r['request_rate'].max():.1f}")
            frames.append(df)
    all_runs = pd.concat(frames, ignore_index=True)
    all_runs.to_csv(args.out, index=False)
    print(f"Wrote {args.out}: {len(all_runs)} rows, SYNTHETIC (practice only)")


if __name__ == "__main__":
    main()
