#!/usr/bin/env python3
"""Export Prometheus metrics for each labelled run made by run_scenarios.py, one row per 15 s.

  python3 export_runs.py                     # every complete run in data/runs/
  python3 export_runs.py attack_flood_r2     # only these runs
  python3 export_runs.py --check             # query the last 10 minutes and say what is missing

Needs the Prometheus port-forward:
  kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090

Writes data/runs/<run>/metrics.csv for each run and data/dataset.csv with all runs together.

Columns
  timestamp          Unix seconds (Prometheus / Docker VM clock)
  replicas, desired_replicas    HPA current and desired replicas
  cpu_utilization    CPU of the hpa-app containers, % of one core, summed over pods
                     (container="hpa-app" only; the old export also counted the pod-level series,
                     which doubled the number, so do not mix old and new data)
  request_rate       requests per second to /work
  latency_p95_s      95th percentile request time (0 when there were no requests)
  inflight           requests being handled at that moment
  memory_mb          working-set memory of the hpa-app containers
  net_rx_kbps, net_tx_kbps      network kilobytes per second in / out of the hpa-app pods
  run, scenario, phase, kind, label (0 normal, 1 attack), event_id, event_start
  in_run             0 for the minute exported before the run (rate windows warming up)
  synthetic          0 here; 1 in files made by simulate_runs.py

Labels come from labels.csv (when the load was injected). Every metric uses a 1-minute rate
window, so it reacts up to a minute after the label changes; evaluate.py measures that as latency.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

PROM = os.getenv("PROM_URL", "http://localhost:9090")
RUNS_DIR = os.path.join("data", "runs")
PRE_ROLL_S = 60

POD = 'namespace="default", pod=~"hpa-app-.*"'
QUERIES = {
    "replicas": 'sum(kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="hpa-app"})',
    "desired_replicas": 'sum(kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="hpa-app"})',
    "cpu_utilization": f'sum(rate(container_cpu_usage_seconds_total{{{POD}, container="hpa-app"}}[1m])) * 100',
    "request_rate": 'sum(rate(app_requests_total{namespace="default"}[1m]))',
    "latency_p95_s": 'histogram_quantile(0.95, sum by (le) (rate(app_request_duration_seconds_bucket{namespace="default"}[1m])))',
    "inflight": 'sum(app_inflight_requests{namespace="default"})',
    "memory_mb": f'sum(container_memory_working_set_bytes{{{POD}, container="hpa-app"}}) / 1e6',
    "net_rx_kbps": f'sum(rate(container_network_receive_bytes_total{{{POD}, interface="eth0"}}[1m])) / 1000',
    "net_tx_kbps": f'sum(rate(container_network_transmit_bytes_total{{{POD}, interface="eth0"}}[1m])) / 1000',
}
# A missing value means "nothing happened" for these (no requests -> no series), so it becomes 0.
ZERO_IF_MISSING = ["request_rate", "latency_p95_s", "inflight", "net_rx_kbps", "net_tx_kbps"]
LABEL_COLUMNS = ["run", "scenario", "phase", "kind", "label", "event_id", "event_start", "in_run", "synthetic"]


def query_range(query, start, end, step):
    r = requests.get(f"{PROM}/api/v1/query_range",
                     params={"query": query, "start": start, "end": end, "step": step}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", body))
    result = body["data"]["result"]
    if not result:
        return pd.Series(dtype=float)
    if len(result) > 1:
        print(f"    warning: {len(result)} series for {query[:60]}..., using the first")
    ts, vals = zip(*result[0]["values"])
    s = pd.Series(pd.to_numeric(vals, errors="coerce"), index=np.round(np.array(ts, dtype=float), 3))
    return s.replace([np.inf, -np.inf], np.nan)


def fetch(start, end, step):
    grid = np.round(np.arange(start, end + step / 2, step), 3)
    df = pd.DataFrame(index=grid)
    for col, q in QUERIES.items():
        df[col] = query_range(q, start, end, step).reindex(grid)
    df[ZERO_IF_MISSING] = df[ZERO_IF_MISSING].fillna(0)
    df.index.name = "timestamp"
    return df.reset_index()


def attack_windows(labels):
    """One window per attack event_id: [first start, last end)."""
    att = labels[labels["label"] == "attack"]
    if att.empty:
        return pd.DataFrame(columns=["event_id", "kind", "start", "end"])
    return (att.groupby("event_id")
            .agg(kind=("kind", "first"), start=("start", "min"), end=("end", "max"))
            .reset_index().sort_values("start"))


def label_rows(df, labels, run_start, run_end):
    """Add phase, kind, label, event_id, event_start, in_run to a metrics frame (by timestamp)."""
    labels = labels.copy()
    labels["event_id"] = labels["event_id"].fillna("").astype(str)
    windows = attack_windows(labels)
    normal = labels[labels["label"] != "attack"].sort_values("start")
    t = df["timestamp"].to_numpy()

    phase = np.full(len(t), "idle", dtype=object)
    kind = np.full(len(t), "idle", dtype=object)
    # normal segments: a later-starting segment (a burst on top of the baseline) wins
    for _, s in normal.iterrows():
        m = (t >= s["start"]) & (t < s["end"])
        phase[m], kind[m] = s["phase"], s["kind"]
    label = np.zeros(len(t), dtype=int)
    event_id = np.full(len(t), "", dtype=object)
    event_start = np.full(len(t), np.nan)
    for _, w in windows.iterrows():
        m = (t >= w["start"]) & (t < w["end"])
        label[m], kind[m], phase[m] = 1, w["kind"], w["event_id"]
        event_id[m], event_start[m] = w["event_id"], w["start"]
    df["phase"], df["kind"], df["label"] = phase, kind, label
    df["event_id"], df["event_start"] = event_id, event_start
    df["in_run"] = ((t >= run_start) & (t <= run_end)).astype(int)
    return df


def load_run(run_dir):
    with open(os.path.join(run_dir, "meta.json")) as f:
        meta = json.load(f)
    labels = pd.read_csv(os.path.join(run_dir, "labels.csv"))
    return meta, labels


def export_run(run_dir, step):
    meta, labels = load_run(run_dir)
    if meta["end"] > time.time() + meta.get("clock_offset_s", 0):
        raise RuntimeError("the run has not finished yet")
    df = fetch(meta["start"] - PRE_ROLL_S, meta["end"], step)
    df = label_rows(df, labels, meta["start"], meta["end"])
    df.insert(df.columns.get_loc("phase"), "scenario", meta["scenario"])
    df.insert(df.columns.get_loc("scenario"), "run", meta["run"])
    df["synthetic"] = int(bool(meta.get("synthetic", False)))
    df.to_csv(os.path.join(run_dir, "metrics.csv"), index=False)
    return df


def sanity(df, name):
    r = df[df["in_run"] == 1]
    notes = []
    if r["replicas"].isna().mean() > 0.2:
        notes.append(f"replicas missing in {r['replicas'].isna().mean():.0%} of rows (is kube-state-metrics up?)")
    if (r["request_rate"] == 0).all():
        notes.append("request_rate is 0 everywhere: check that app_requests_total returns data in Prometheus")
    if r["cpu_utilization"].isna().mean() > 0.2:
        notes.append("cpu_utilization mostly missing (check the container name, should be hpa-app)")
    for n in notes:
        print(f"    WARNING {name}: {n}")
    return not notes


def check(step):
    end = time.time()
    df = fetch(end - 600, end, step)
    print("Last 10 minutes, share of rows with a value / max:")
    for col in QUERIES:
        v = df[col]
        print(f"  {col:<18} {v.notna().mean():>5.0%}   max={v.max():.3g}")
    df["in_run"] = 1
    if sanity(df, "check"):
        print("Looks fine.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="*", help="run names (default: all complete runs)")
    p.add_argument("--runs-dir", default=RUNS_DIR)
    p.add_argument("--out", default=os.path.join("data", "dataset.csv"))
    p.add_argument("--step", type=int, default=15, help="seconds between rows")
    p.add_argument("--check", action="store_true", help="only test the queries on the last 10 minutes")
    args = p.parse_args()

    try:
        if args.check:
            return check(args.step)
        names = args.runs or sorted(d for d in os.listdir(args.runs_dir)
                                    if os.path.isfile(os.path.join(args.runs_dir, d, "meta.json")))
    except requests.ConnectionError:
        sys.exit(f"Cannot reach Prometheus at {PROM}. Is the port-forward running?")
    except FileNotFoundError:
        sys.exit(f"No {args.runs_dir} folder yet. Run run_scenarios.py first.")

    frames = []
    for name in names:
        run_dir = os.path.join(args.runs_dir, name)
        try:
            meta, _ = load_run(run_dir)
            if not meta.get("complete", False):
                print(f"  {name}: skipped, the run did not finish (interrupted?)")
                continue
            df = export_run(run_dir, args.step)
        except requests.ConnectionError:
            sys.exit(f"Cannot reach Prometheus at {PROM}. Is the port-forward running?")
        except (OSError, RuntimeError, KeyError, requests.HTTPError) as e:
            print(f"  {name}: skipped ({e})")
            continue
        r = df[df["in_run"] == 1]
        print(f"  {name:<22} {len(r):>4} rows  attack rows {r['label'].sum():>3}  "
              f"max replicas {r['replicas'].max():.0f}  max cpu {r['cpu_utilization'].max():.0f}%  "
              f"max req/s {r['request_rate'].max():.1f}")
        sanity(df, name)
        frames.append(df)
    if not frames:
        sys.exit("Nothing exported.")
    all_runs = pd.concat(frames, ignore_index=True)
    all_runs.to_csv(args.out, index=False)
    print(f"Wrote {args.out}: {len(all_runs)} rows from {len(frames)} runs "
          f"({(all_runs['in_run'] == 1).sum()} inside the runs)")


if __name__ == "__main__":
    main()
