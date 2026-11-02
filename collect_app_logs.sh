#!/usr/bin/env bash
# Stream the access log of every hpa-app pod into its own file, including pods created later by
# the autoscaler. A pod's logs disappear when the pod is deleted, so this must run DURING the test.
#
#   ./collect_app_logs.sh                 # writes data/logs/<pod-name>.log, Ctrl+C to stop
#   LABEL=app=hpa-app NS=default OUT=data/logs ./collect_app_logs.sh    # the defaults
#
# Start it before the load, leave it running until the pods have scaled back down. Works with the
# bash that ships with macOS (3.2).
set -u

LABEL="${LABEL:-app=hpa-app}"
NS="${NS:-default}"
OUT="${OUT:-data/logs}"
POLL="${POLL:-3}"   # seconds between checks for new pods

mkdir -p "$OUT/.pids"
: > "$OUT/.errors"

cleanup() {
  for pf in "$OUT"/.pids/*; do
    [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null
  done
  rm -rf "$OUT/.pids"
  echo
  echo "Stopped. Lines per pod:"
  for f in "$OUT"/*.log; do
    [ -f "$f" ] && printf '  %-50s %s\n' "$(basename "$f")" "$(wc -l < "$f" | tr -d ' ')"
  done
  [ -s "$OUT/.errors" ] && echo "Some streams reported errors, see $OUT/.errors"
  exit 0
}
trap cleanup INT TERM

echo "Collecting logs for pods with label $LABEL in namespace $NS into $OUT (Ctrl+C to stop)"

while true; do
  pods="$(kubectl get pods -n "$NS" -l "$LABEL" --field-selector=status.phase=Running -o name 2>>"$OUT/.errors" | sed 's#^pod/##')"
  for pod in $pods; do
    pidfile="$OUT/.pids/$pod"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      continue   # already streaming
    fi
    # Not streaming yet, or the stream dropped while the pod is still running. A restarted stream
    # repeats earlier lines; parse_access_logs.py drops the repeats.
    kubectl logs -n "$NS" -f --tail=-1 "$pod" >> "$OUT/$pod.log" 2>>"$OUT/.errors" &
    echo $! > "$pidfile"
    echo "  streaming $pod"
  done
  sleep "$POLL"
done