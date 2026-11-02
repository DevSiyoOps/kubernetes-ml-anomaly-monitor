#!/usr/bin/env bash
# Capture the app's network traffic from inside the kind node and save a pod-IP map.
#
#   ./capture_network.sh 900          # capture for 900 seconds (15 minutes)
#   ./capture_network.sh 900 8080     # second argument = the port the app listens on in the pod
#
# Start this BEFORE the load test, then run the load while it is capturing (use a load pod INSIDE the
# cluster: kubectl port-forward traffic never reaches the node's network interfaces).
# Output: data/capture.pcap and data/pod_ips.txt  (feed both to pcap_to_flows.py)
#
# Why this captures each packet once: the filter is on the pod's own port (8080). A request is
# seen when it leaves the node toward the app pod (after the Service rewrites the address), and a
# reply is seen when it comes back from the pod. The same packet is not matched a second time on
# the client's side, where the port is the Service port (80).
#
# The pod-IP map is refreshed every 5 seconds during the capture, so pods that the autoscaler
# creates and removes mid-run still get a name. Other pods that listen on 8080 (kube-state-metrics)
# are captured too; pcap_to_flows.py --server-match hpa-app drops them.
set -euo pipefail

DURATION="${1:?usage: $0 <seconds> [app-port]}"
APP_PORT="${2:-8080}"
NODE="${KIND_NODE:-hpa-lab-control-plane}"

mkdir -p data

# tcpdump is not in the kind node image; install it once (about 10 MB).
if ! docker exec "$NODE" sh -c 'command -v tcpdump >/dev/null 2>&1'; then
  echo "Installing tcpdump inside $NODE ..."
  docker exec "$NODE" sh -c 'apt-get update -qq && apt-get install -y -qq tcpdump' >/dev/null
fi

pod_map() {
  kubectl get pods -A -o custom-columns=IP:.status.podIP,NS:.metadata.namespace,NAME:.metadata.name --no-headers 2>/dev/null
}

RAW="data/.pod_ips_raw.txt"
: > "$RAW"
pod_map >> "$RAW"

# Keep sampling the pod list in the background until the capture ends.
(
  while true; do
    sleep 5
    pod_map >> "$RAW"
  done
) &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null || true' EXIT

echo "Capturing TCP port $APP_PORT for ${DURATION}s on $NODE. Start your load test now. Output: data/capture.pcap"

# -s 128 keeps headers only (no request bodies); -U writes each packet immediately; 'timeout' ends the capture.
docker exec "$NODE" timeout "$DURATION" tcpdump -i any -nn -U -s 128 -w - "tcp port $APP_PORT" > data/capture.pcap || true

kill "$SAMPLER" 2>/dev/null || true
pod_map >> "$RAW"
# one line per distinct pod; an IP that was reused by a later pod keeps the first name seen
awk 'NF >= 3 && $1 != "<none>" && !seen[$1 " " $3]++' "$RAW" > data/pod_ips.txt
rm -f "$RAW"

echo "Done. $(du -h data/capture.pcap | cut -f1) captured, $(wc -l < data/pod_ips.txt | tr -d ' ') pod addresses recorded."
echo "Next: python3 pcap_to_flows.py data/capture.pcap --pod-map data/pod_ips.txt --server-match hpa-app"