#!/usr/bin/env python3
"""Turn a tcpdump capture of the app's traffic into network flow logs and per-interval features.

Outputs
  flows CSV     one row per connection: when, client IP/port, server IP/port, protocol, packets and
                bytes each way, SYN/FIN/RST counts, and pod names when a pod map is given
  features CSV  one row per time bin (default 15 s): packets/s, bytes/s, new connections/s,
                resets/s, distinct source IPs, share of traffic from the busiest source
  merged CSV    (optional) the features joined onto the metrics CSV from export_metrics.py

Capture first (see capture_network.sh), then:

  python3 pcap_to_flows.py data/capture.pcap --pod-map data/pod_ips.txt \\
      --metrics data/anomaly_run.csv --merged-out data/anomaly_run_with_network.csv

Needs only the Python standard library, plus pandas for the features and the merge.
"""
import argparse
import csv
import os
import socket
import struct
import sys
from collections import Counter
from datetime import datetime, timezone

PROTO_NAMES = {1: "ICMP", 6: "TCP", 17: "UDP", 47: "GRE", 50: "ESP", 58: "ICMPv6", 132: "SCTP"}
MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6),
    b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9),
    b"\xa1\xb2\x3c\x4d": (">", 1e-9),
}


def open_pcap(path):
    """Return (linktype, packet iterator, open file) for a classic pcap file."""
    f = open(path, "rb")
    head = f.read(24)
    if len(head) < 24:
        sys.exit("The file is too short to be a pcap capture.")
    if head[:4] == b"\x0a\x0d\x0d\x0a":
        sys.exit("This is a pcapng file. Capture with tcpdump -w (classic pcap), "
                 "or convert it: editcap -F pcap in.pcapng out.pcap")
    if head[:4] not in MAGICS:
        sys.exit("Not a pcap file (unknown magic number).")
    endian, scale = MAGICS[head[:4]]
    linktype = struct.unpack(endian + "HHiIII", head[4:24])[5]
    record = struct.Struct(endian + "IIII")

    def packets():
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            sec, frac, incl, _orig = record.unpack(h)
            if incl > 262144:  # corrupt record header
                return
            data = f.read(incl)
            if len(data) < incl:  # capture was cut off mid-packet
                return
            yield sec + frac * scale, data

    return linktype, packets(), f


def strip_link_layer(linktype, data):
    """Return (bytes starting at the IP header, ethertype). ethertype 0 means 'not IP'."""
    if linktype == 1:  # Ethernet
        if len(data) < 14:
            return b"", 0
        ethertype = struct.unpack("!H", data[12:14])[0]
        offset = 14
        while ethertype in (0x8100, 0x88A8) and len(data) >= offset + 4:  # VLAN tags
            ethertype = struct.unpack("!H", data[offset + 2:offset + 4])[0]
            offset += 4
        return data[offset:], ethertype
    if linktype == 113:  # Linux cooked capture v1 (tcpdump -i any on older libpcap)
        if len(data) < 16:
            return b"", 0
        return data[16:], struct.unpack("!H", data[14:16])[0]
    if linktype == 276:  # Linux cooked capture v2 (tcpdump -i any on libpcap 1.10+)
        if len(data) < 20:
            return b"", 0
        return data[20:], struct.unpack("!H", data[0:2])[0]
    if linktype in (101, 12, 228):  # raw IP
        if not data:
            return b"", 0
        version = data[0] >> 4
        return data, (0x0800 if version == 4 else 0x86DD if version == 6 else 0)
    sys.exit("Unsupported link type %d (supported: Ethernet, Linux cooked v1/v2, raw IP)." % linktype)


def parse_ipv4(b):
    """Return (proto, src, sport, dst, dport, tcp_flags, ip_bytes, payload_bytes) or None."""
    if len(b) < 20 or (b[0] >> 4) != 4:
        return None
    ihl = (b[0] & 15) * 4
    if ihl < 20 or len(b) < ihl:
        return None
    total = struct.unpack("!H", b[2:4])[0]
    fragment_offset = struct.unpack("!H", b[6:8])[0] & 0x1FFF
    proto = b[9]
    src = socket.inet_ntoa(b[12:16])
    dst = socket.inet_ntoa(b[16:20])
    sport = dport = flags = payload = 0
    l4 = b[ihl:]
    if fragment_offset == 0:
        if proto == 6 and len(l4) >= 14:
            sport, dport = struct.unpack("!HH", l4[:4])
            flags = l4[13]
            payload = max(total - ihl - (l4[12] >> 4) * 4, 0)
        elif proto == 17 and len(l4) >= 8:
            sport, dport = struct.unpack("!HH", l4[:4])
            payload = max(total - ihl - 8, 0)
    return proto, src, sport, dst, dport, flags, total, payload


def orient(proto, src, sport, dst, dport, app_port):
    """Decide which end is the client. Returns ((client_ip, client_port, server_ip, server_port), 'c2s'|'s2c')."""
    if proto in (6, 17):
        if dport == app_port:
            return (src, sport, dst, dport), "c2s"
        if sport == app_port:
            return (dst, dport, src, sport), "s2c"
    return (src, sport, dst, dport), "c2s"


def load_pod_map(path):
    """Read 'IP NAMESPACE NAME' lines (kubectl get pods -A -o custom-columns=... --no-headers)."""
    names = {}
    if not path:
        return names
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] != "<none>":
                names.setdefault(parts[0], "%s/%s" % (parts[1], parts[2]))
    return names


def utc_string(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pcap", help="capture file written by tcpdump -w")
    p.add_argument("--app-port", type=int, default=8080, help="port the app listens on inside the pod")
    p.add_argument("--step", type=int, default=15, help="feature bin size in seconds (match the export step)")
    p.add_argument("--pod-map", default=None, help="file with 'IP NAMESPACE NAME' lines to label IPs")
    p.add_argument("--server-match", default=None, metavar="TEXT",
                   help="keep only traffic to pods whose name contains TEXT (needs --pod-map), e.g. hpa-app")
    p.add_argument("--flows-out", default="data/flows.csv")
    p.add_argument("--features-out", default="data/net_features.csv")
    p.add_argument("--metrics", default=None, help="metrics CSV from export_metrics.py to merge features into")
    p.add_argument("--merged-out", default="data/anomaly_run_with_network.csv")
    args = p.parse_args()

    names = load_pod_map(args.pod_map)
    linktype, packets, handle = open_pcap(args.pcap)

    flows = {}
    bins = {}
    totals = Counter()
    proto_counts = Counter()
    sources = Counter()
    first_ts = last_ts = None

    try:
        for ts, data in packets:
            totals["packets read"] += 1
            ip_bytes, ethertype = strip_link_layer(linktype, data)
            if ethertype == 0x86DD:
                totals["IPv6 skipped"] += 1
                continue
            if ethertype != 0x0800:
                totals["non-IP skipped"] += 1
                continue
            parsed = parse_ipv4(ip_bytes)
            if parsed is None:
                totals["unparseable skipped"] += 1
                continue
            proto, src, sport, dst, dport, flags, ip_len, payload = parsed
            (cip, cport, sip, sport_s), direction = orient(proto, src, sport, dst, dport, args.app_port)
            # Other pods also listen on port 8080 (kube-state-metrics, for one). --server-match keeps
            # only traffic whose server pod name contains the given text, e.g. hpa-app.
            if args.server_match and args.server_match not in names.get(sip, ""):
                totals["other servers skipped"] += 1
                continue
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
            proto_counts[PROTO_NAMES.get(proto, "proto-%d" % proto)] += 1

            # --- flow log ---
            key = (proto, cip, cport, sip, sport_s)
            flow = flows.get(key)
            if flow is None:
                flow = flows[key] = {"start": ts, "end": ts, "c_pkts": 0, "c_bytes": 0,
                                     "s_pkts": 0, "s_bytes": 0, "syn": 0, "fin": 0, "rst": 0}
            flow["end"] = max(flow["end"], ts)
            flow["start"] = min(flow["start"], ts)
            if direction == "c2s":
                flow["c_pkts"] += 1
                flow["c_bytes"] += ip_len
            else:
                flow["s_pkts"] += 1
                flow["s_bytes"] += ip_len
            if proto == 6:
                if flags & 0x02:
                    flow["syn"] += 1
                if flags & 0x01:
                    flow["fin"] += 1
                if flags & 0x04:
                    flow["rst"] += 1

            # --- per-interval features ---
            b = bins.get(int(ts // args.step) * args.step)
            if b is None:
                b = bins[int(ts // args.step) * args.step] = {
                    "in_pkts": 0, "in_bytes": 0, "out_pkts": 0, "out_bytes": 0,
                    "new_conns": 0, "rst": 0, "non_tcp": 0, "src": Counter()}
            if proto in (6, 17) and direction == "c2s" and dport == args.app_port:
                b["in_pkts"] += 1
                b["in_bytes"] += ip_len
                b["src"][src] += 1
                sources[src] += 1
                if proto == 6 and (flags & 0x02) and not (flags & 0x10):
                    b["new_conns"] += 1
                if proto == 17:
                    b["non_tcp"] += 1
            elif proto in (6, 17) and direction == "s2c" and sport == args.app_port:
                b["out_pkts"] += 1
                b["out_bytes"] += ip_len
            else:
                b["non_tcp"] += 1
            if proto == 6 and (flags & 0x04):
                b["rst"] += 1
    finally:
        handle.close()

    if first_ts is None:
        if totals["other servers skipped"]:
            sys.exit("--server-match '%s' matched no traffic. Does the pod map (--pod-map) list the app's pod IPs? "
                     "Traffic seen: %d packets to other servers." % (args.server_match, totals["other servers skipped"]))
        sys.exit("No IPv4 packets found. Was the capture filter right, and was traffic flowing during the capture?")

    # --- write the flow log ---
    for out_path in (args.flows_out, args.features_out, args.merged_out):
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(args.flows_out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start_epoch", "start_utc", "duration_s", "protocol", "client_ip", "client_port",
                    "server_ip", "server_port", "client_name", "server_name",
                    "client_packets", "client_bytes", "server_packets", "server_bytes",
                    "syn", "fin", "rst"])
        for (proto, cip, cport, sip, sport_s), fl in sorted(flows.items(), key=lambda kv: kv[1]["start"]):
            w.writerow([round(fl["start"], 3), utc_string(fl["start"]), round(fl["end"] - fl["start"], 3),
                        PROTO_NAMES.get(proto, "proto-%d" % proto), cip, cport, sip, sport_s,
                        names.get(cip, ""), names.get(sip, ""),
                        fl["c_pkts"], fl["c_bytes"], fl["s_pkts"], fl["s_bytes"],
                        fl["syn"], fl["fin"], fl["rst"]])

    # --- write the features (every bin between first and last packet, empty bins as zero traffic) ---
    import pandas as pd
    start_bin = int(first_ts // args.step) * args.step
    end_bin = int(last_ts // args.step) * args.step
    rows = []
    for t in range(start_bin, end_bin + args.step, args.step):
        b = bins.get(t)
        if b is None:
            rows.append([float(t), 0, 0, 0, 0, 0, 0, 0, 0, 0.0])
            continue
        top_share = (max(b["src"].values()) / b["in_pkts"]) if b["in_pkts"] else 0.0
        rows.append([float(t), b["in_pkts"] / args.step, b["in_bytes"] / args.step,
                     b["out_pkts"] / args.step, b["out_bytes"] / args.step,
                     b["new_conns"] / args.step, b["rst"] / args.step,
                     len(b["src"]), b["non_tcp"] / args.step, round(top_share, 4)])
    features = pd.DataFrame(rows, columns=[
        "timestamp", "net_pkts_in_s", "net_bytes_in_s", "net_pkts_out_s", "net_bytes_out_s",
        "net_new_conns_s", "net_rst_s", "net_unique_src", "net_non_tcp_s", "net_top_src_share"])
    features.to_csv(args.features_out, index=False)

    # --- summary ---
    span = last_ts - first_ts
    print("Capture: %s to %s UTC (%.1f min), link type %d" % (
        utc_string(first_ts), utc_string(last_ts), span / 60.0, linktype))
    print("Packets: %s" % ", ".join("%s %d" % (k, v) for k, v in totals.items()))
    print("Protocols: %s" % ", ".join("%s %d" % (k, v) for k, v in proto_counts.most_common()))
    print("Flows: %d  ->  %s" % (len(flows), args.flows_out))
    print("Feature rows: %d (%ds bins)  ->  %s" % (len(features), args.step, args.features_out))
    total_in = sum(sources.values())
    if total_in:
        print("\nTop sources of traffic to port %d:" % args.app_port)
        for ip, n in sources.most_common(8):
            print("  %-16s %-34s %7d packets  %5.1f%%" % (ip, names.get(ip, ""), n, 100.0 * n / total_in))

    if args.metrics:
        metrics = pd.read_csv(args.metrics).sort_values("timestamp")
        merged = pd.merge_asof(metrics, features.sort_values("timestamp"), on="timestamp",
                               direction="nearest", tolerance=args.step / 2.0)
        merged.to_csv(args.merged_out, index=False)
        coverage = merged["net_pkts_in_s"].notna().mean()
        print("\nMerged into %s: %d rows x %d columns, %.0f%% of rows have network features "
              "(rows outside the capture window are blank)." % (
                  args.merged_out, merged.shape[0], merged.shape[1], coverage * 100))


if __name__ == "__main__":
    main()