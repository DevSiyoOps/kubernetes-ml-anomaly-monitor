#!/usr/bin/env python3
"""Turn the app's access logs (JSON lines from app v3, or v2) into a per-request table, a summary of
who connected and how, and one feature row per time bin that can be merged with the metrics CSV.

Get the logs first. Pods that scale down take their logs with them, so stream them DURING the run:

  ./collect_app_logs.sh            # leave running in its own tab; Ctrl+C when the run is over

or, after a short run, while the pods still exist:

  kubectl logs -l app=hpa-app --prefix --tail=-1 > data/logs/all.log

Then:

  python3 parse_access_logs.py data/logs --ip-map data/pod_ips.txt \\
      --metrics data/anomaly_run.csv --out data/anomaly_run_with_logs.csv

  data/logs can be a folder, a file, or several of either. --ip-map is optional: it adds pod names
  next to the IPs (make it with: kubectl get pods -A -o wide, or capture_network.sh does it for you).

Writes
  data/requests.csv          one row per request: time, client IP and port, server IP and port,
                             protocol, method, path, status, bytes, duration, user agent, ...
  --out                      one row per time bin (features), merged onto --metrics if given
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# column names written by app v2 -> the names app v3 uses
V2_NAMES = {"client": "client_ip", "bytes": "resp_bytes"}
REQUEST_COLUMNS = [
    "time_utc", "ts", "pod", "client_ip", "client_name", "client_port", "server_ip", "server_port",
    "transport", "proto", "scheme", "method", "path", "query", "status", "req_body_bytes",
    "resp_bytes", "duration_ms", "host", "user_agent", "xff", "connection", "request_id",
]


def list_files(inputs):
    files = []
    for item in inputs:
        if os.path.isdir(item):
            files += sorted(glob.glob(os.path.join(item, "*.log")))
        else:
            files += sorted(glob.glob(item)) or [item]
    return [f for f in files if os.path.isfile(f)]


def read_logs(files):
    rows, skipped = [], 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                start = line.find("{")  # skips the "[pod/name/container]" prefix from --prefix
                if start < 0:
                    continue
                try:
                    rec = json.loads(line[start:])
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(rec, dict) and "ts" in rec:
                    rows.append(rec)
    df = pd.DataFrame(rows)
    return df, skipped


def load_ip_map(path):
    """Read 'IP NAMESPACE NAME' lines (kubectl get pods -A -o custom-columns=IP:...,NS:...,NAME:... --no-headers)."""
    names = {}
    if not path:
        return names
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] != "<none>":
                names.setdefault(parts[0], "%s/%s" % (parts[1], parts[2]))
    return names


def clean(df, names):
    df = df.rename(columns={k: v for k, v in V2_NAMES.items() if k in df.columns and v not in df.columns})
    for col in REQUEST_COLUMNS:
        if col not in df.columns and col not in ("time_utc", "client_name"):
            df[col] = np.nan
    before = len(df)
    df = df.drop_duplicates()  # a restarted log stream repeats lines
    df = df.sort_values("ts").reset_index(drop=True)
    df["time_utc"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
    df["client_name"] = df["client_ip"].map(names).fillna("")
    for col in ("status", "client_port", "server_port"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[REQUEST_COLUMNS], before - len(df)


def build_features(df, step):
    d = df.copy()
    d["bin"] = (d["ts"] // step) * step
    g = d.groupby("bin")

    def top_share(series):
        counts = series.value_counts()
        return counts.iloc[0] / counts.sum() if len(counts) else np.nan

    out = pd.DataFrame(
        {
            "log_req_per_s": g.size() / step,
            "log_bytes_in_per_s": g["req_body_bytes"].sum() / step,
            "log_bytes_out_per_s": g["resp_bytes"].sum() / step,
            "log_mean_ms": g["duration_ms"].mean(),
            "log_p95_ms": g["duration_ms"].quantile(0.95),
            "log_errors": g["status"].apply(lambda s: int((s >= 500).sum())),
            "log_client_errors": g["status"].apply(lambda s: int(((s >= 400) & (s < 500)).sum())),
            "log_unique_src_ips": g["client_ip"].nunique(),
            "log_top_src_share": g["client_ip"].apply(top_share),
            "log_active_pods": g["pod"].nunique(),
        }
    )
    full_range = np.arange(out.index.min(), out.index.max() + step, step)
    out = out.reindex(full_range)
    # inside the logged range, a bin with no log lines means no requests
    for col in ["log_req_per_s", "log_bytes_in_per_s", "log_bytes_out_per_s", "log_errors",
                "log_client_errors", "log_unique_src_ips"]:
        out[col] = out[col].fillna(0)
    out.index.name = "timestamp"
    return out.reset_index()


def show_summary(df, names):
    first = datetime.fromtimestamp(df["ts"].min(), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    last = datetime.fromtimestamp(df["ts"].max(), timezone.utc).strftime("%H:%M:%S")
    print("\nRequests: %d from %d source IP(s) to %d pod(s), %s to %s UTC" % (
        len(df), df["client_ip"].nunique(), df["pod"].nunique(), first, last))

    print("\nSource IPs")
    for ip, n in df["client_ip"].value_counts().head(10).items():
        sub = df[df["client_ip"] == ip]
        print("  %-16s %-30s %7d requests  %5.1f%%  mean %.0f ms  %d distinct TCP ports" % (
            ip, names.get(ip, ""), n, 100.0 * n / len(df), sub["duration_ms"].mean(),
            sub["client_port"].nunique()))

    for title, col in (("Protocol", "proto"), ("Method", "method"), ("Status", "status"),
                       ("Path", "path"), ("User agent", "user_agent"), ("Destination", None)):
        print("\n" + title)
        if col is None:
            counts = (df["server_ip"].astype(str) + ":" + df["server_port"].astype("Int64").astype(str)).value_counts()
        else:
            counts = df[col].astype(str).value_counts()
        for value, n in counts.head(6).items():
            print("  %-40s %7d" % (value[:40], n))
    forwarded = df["xff"].dropna()
    if len(forwarded):
        print("\nX-Forwarded-For was set on %d requests (a client can forge it; client_ip is the real peer)." % len(forwarded))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("logs", nargs="+", help="log file(s) or folder(s) of *.log files")
    p.add_argument("--step", type=int, default=15, help="bin size in seconds (match the export step)")
    p.add_argument("--ip-map", default=None, help="'IP NAMESPACE NAME' lines to put pod names next to IPs")
    p.add_argument("--metrics", default=None, help="metrics CSV from export_metrics.py to merge into")
    p.add_argument("--requests-out", default="data/requests.csv", help="per-request table")
    p.add_argument("--out", default="data/access_log_features.csv", help="per-bin features (or merged metrics)")
    p.add_argument("--pretty", type=int, default=0, metavar="N", help="also print the first N requests as readable lines")
    args = p.parse_args()

    files = list_files(args.logs)
    if not files:
        sys.exit("No log files found in: %s" % " ".join(args.logs))
    raw, skipped = read_logs(files)
    if raw.empty:
        sys.exit("No JSON log lines found in %d file(s). Is app v3 deployed, and did the load hit /work?" % len(files))
    names = load_ip_map(args.ip_map)
    df, dupes = clean(raw, names)
    print("Read %d requests from %d file(s)%s%s" % (
        len(df), len(files),
        (", dropped %d repeated lines" % dupes) if dupes else "",
        (", skipped %d unreadable lines" % skipped) if skipped else ""))

    for path in (args.requests_out, args.out):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(args.requests_out, index=False)
    print("Saved %s" % args.requests_out)

    if args.pretty:
        print()
        for r in df.head(args.pretty).itertuples():
            print("%s  %s:%s -> %s:%s  %s %s  %s %s%s  %s  %dB  %.0f ms  ua=%s" % (
                r.time_utc, r.client_ip, int(r.client_port) if pd.notna(r.client_port) else "?",
                r.server_ip, int(r.server_port) if pd.notna(r.server_port) else "?",
                r.transport if pd.notna(r.transport) else "TCP", r.proto, r.method, r.path,
                ("?" + r.query) if isinstance(r.query, str) and r.query else "",
                int(r.status), r.resp_bytes if pd.notna(r.resp_bytes) else 0, r.duration_ms, r.user_agent))

    show_summary(df, names)

    feats = build_features(df, args.step)
    if args.metrics:
        metrics = pd.read_csv(args.metrics).sort_values("timestamp")
        merged = pd.merge_asof(metrics, feats.sort_values("timestamp"), on="timestamp",
                               direction="nearest", tolerance=args.step / 2.0)
        merged.to_csv(args.out, index=False)
        covered = merged["log_req_per_s"].notna().mean()
        print("\nSaved %s: %d rows x %d columns (metrics + log features). %.0f%% of rows have log "
              "features; rows outside the log range are blank." % (args.out, merged.shape[0], merged.shape[1], covered * 100))
    else:
        feats.to_csv(args.out, index=False)
        print("\nSaved %s: %d rows x %d columns" % (args.out, *feats.shape))


if __name__ == "__main__":
    main()