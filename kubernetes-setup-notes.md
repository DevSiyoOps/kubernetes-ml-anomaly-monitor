# Kubernetes HPA Lab: Setup Command Log

Project: ML-Based Anomaly Detection for Kubernetes Autoscaling (SENG 5070)
Machine: MacBook Air (zsh), Docker Desktop + kind
Cluster name: `hpa-lab`

Status key: [done] confirmed working | [run] commands given, result not confirmed yet | [todo] not started

---

## Stage 0: Tools [done]

```bash
# Docker Desktop installed manually (do NOT also run: brew install --cask docker)
docker info            # should print details, no error
kind version
kubectl version --client
helm version           # if missing: brew install helm
```

Docker Desktop settings: Resources > at least 4 GB RAM, 2 to 4 CPUs.

Tip: zsh rejects `#` comment lines when pasted. Either paste only the commands, or run once per terminal:

```bash
setopt interactive_comments
```

---

## Stage 1: Create the cluster [done]

```bash
kind create cluster --name hpa-lab
kubectl get nodes      # wait until STATUS is Ready
```

---

## Stage 2: metrics-server and the demo app [done]

Confirmed 2026-10-14: metrics-server pod 1/1 Running, `kubectl top nodes` returns numbers, HPA shows `cpu: 249%/50%` with 4 replicas (it scaled up from 1 on its own).

Note: `Metrics API not available` and `<unknown>/50%` right after install are normal. Wait about 1 minute.

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# kind needs this flag, real clusters do not
kubectl patch -n kube-system deployment metrics-server --type=json -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

kubectl apply -f https://k8s.io/examples/application/php-apache.yaml
kubectl autoscale deployment php-apache --cpu=50% --min=1 --max=10
```

Verify (wait about 1 minute):

```bash
kubectl top nodes      # should show CPU and memory numbers
kubectl get hpa        # <unknown> at first, then something like 0%/50%
```

---

## Stage 3: First load test [partly done]

First short run worked: load reached the app and HPA scaled 1 to 4 replicas. Still to do: a clean run with `kubectl get hpa -w` open in tab 1 so the full scale-up and scale-down is captured for a screenshot.

Terminal tab 1 (load generator):

```bash
kubectl run -i --tty load-generator --rm --image=busybox:1.28 --restart=Never -- /bin/sh -c "while sleep 0.01; do wget -q -O- http://php-apache; done"
```

Terminal tab 2 (watch scaling):

```bash
kubectl get hpa -w
```

Replicas should climb toward 10. Press Ctrl+C in tab 1 to stop the load. Scale-down takes about 5 minutes (default stabilization window). Take a screenshot as checkpoint evidence.

---

## Stage 4: Prometheus [done]

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace --set prometheus.prometheusSpec.scrapeInterval=15s --set alertmanager.enabled=false --set grafana.enabled=false
kubectl get pods -n monitoring -w       # wait until all Running, then Ctrl+C
```

Alertmanager and Grafana are disabled to save memory. Scrape interval is 15 s.

Keep this running in its own terminal tab, then open http://localhost:9090:

```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090
```

Confirmed 2026-10-14: `kube_horizontalpodautoscaler_status_current_replicas` returns the php-apache HPA (value 5 after the first load).

Queries used for the dataset (one value per timestamp each):

```
sum(kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="php-apache"})
sum(kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="php-apache"})
sum(rate(container_cpu_usage_seconds_total{namespace="default", pod=~"php-apache.*", container="php-apache"}[1m]))
sum(container_memory_working_set_bytes{namespace="default", pod=~"php-apache.*", container="php-apache"})
sum(rate(container_network_receive_bytes_total{namespace="default", pod=~"php-apache.*", interface="eth0"}[1m]))
```

Data issues found so far (for the checkpoint):

- Raw cAdvisor network counters returned 45 series (one per interface per pod) and are cumulative, so they need `rate()` and aggregation. Fixed by filtering to `eth0` and summing.
- The php-apache demo app has no request-rate metric. Network receive rate stands in for traffic for now. Plan: replace the demo app with a small custom app that exposes request counters.
- Prometheus was installed after the first load test, so that first run was not captured.

---

## Stage 5: Export data to pandas [export works, plot not confirmed]

Confirmed 2026-10-14: `export_metrics.py` saved `data/run1.csv` (35 rows x 6 columns, 15 s steps). The first run was mostly idle data (5 replicas, 0 network traffic) because it only covered the minutes after Prometheus started, so a proper load run still needs to be captured.

Notes from the first export:

- The first row has NaN in replicas and CPU: the range starts before the first scrape and the 1-minute rate window has no data yet. Drop it in cleaning.
- Timestamps are UTC (03:16 UTC = 20:16 Pacific). Keep this in mind when labeling anomaly windows.
- The `NotOpenSSLWarning` from urllib3 is harmless (macOS system Python uses LibreSSL).
- The scripts were created by pasting `cat > file << 'EOF'` blocks into the terminal in the MLproject folder.

Files: `export_metrics.py` (pulls the five queries above from Prometheus into a CSV) and `load_and_plot.py` (loads the CSV, prints shape/head/info, saves `replicas_plot.png`). Put both in the MLproject folder.

```bash
pip3 install pandas requests matplotlib
python3 export_metrics.py --minutes 20 --run normal_run1 --out data/run1.csv
python3 load_and_plot.py data/run1.csv
```

The port-forward from stage 4 must be running during the export. The `--run` name is stored in a column so later runs (e.g. `yoyo_run1`) can be told apart and labeled as anomalous.

---

## Stage 6: Custom app with request rate [done, app v3 deployed]

Kit contents (`hpa-lab-kit.zip`): `app/app.py` (Flask app, `/work?ms=N` burns CPU, exposes `app_requests_total`), `app/workload.py`, `app/Dockerfile`, `k8s/hpa-app.yaml` (Deployment + Service + ServiceMonitor + HPA), and new versions of `export_metrics.py` and `load_and_plot.py`.

Run these from the MLproject folder:

```bash
unzip -o ~/Downloads/hpa-lab-kit.zip        # overwrites the old export/plot scripts

# remove the old demo app (frees memory)
kubectl delete hpa php-apache
kubectl delete deployment php-apache
kubectl delete service php-apache

# build the image and load it into kind (Docker Desktop must be running)
docker build -t hpa-app:v1 app/
kind load docker-image hpa-app:v1 --name hpa-lab

# if "kind load" fails (Docker Desktop containerd image store), use this instead:
docker save hpa-app:v1 -o hpa-app.tar && kind load image-archive hpa-app.tar --name hpa-lab

kubectl apply -f k8s/hpa-app.yaml
kubectl get pods,hpa,servicemonitor
```

Verify:

```bash
kubectl run curl-test --rm -i --restart=Never --image=busybox:1.28 -- wget -qO- "http://hpa-app/work?ms=20"
```

In Prometheus (http://localhost:9090) the target `hpa-app` should appear under Status > Targets within about a minute, and `app_requests_total` should return data.

HPA settings in the manifest: CPU target 50%, 1 to 10 replicas, scale-down stabilization 120 s (Kubernetes default is 300 s). Record the value used for each run, since the proposal varies HPA settings.

---

## Stage 6b: Network-level logs (who connected, from which IP, which protocol) [app v3 done, log collection not yet run]

Files: `app/app.py` (v3), `collect_app_logs.sh`, `capture_network.sh`, `parse_access_logs.py`, `pcap_to_flows.py`. Put them in the MLproject folder (`app/app.py` replaces the old one). Run `chmod +x *.sh` once.

Two layers of logs:
- App request log (one JSON line per request): client IP and port, server IP and port, TCP, HTTP version, method, path, status, bytes, duration, user agent, X-Forwarded-For.
- Packet flows (tcpdump inside the kind node): one row per TCP connection with packets and bytes each way and SYN/FIN/RST counts.

Rebuild and roll out app v3:

```bash
docker build -t hpa-app:v3 app/
kind load docker-image hpa-app:v3 --name hpa-lab
# if "kind load" fails, use this instead:
docker save hpa-app:v3 -o hpa-app.tar && kind load image-archive hpa-app.tar --name hpa-lab

kubectl get deployment hpa-app -o jsonpath='{.spec.template.spec.containers[0].name}'   # container name
kubectl set image deployment/hpa-app <container-name>=hpa-app:v3
kubectl rollout status deployment/hpa-app
```

Collect during a run (three terminal tabs, start 1 and 2 BEFORE the load):

```bash
./collect_app_logs.sh            # tab 1: streams every hpa-app pod log to data/logs/, Ctrl+C at the end
./capture_network.sh 900         # tab 2: 15 min packet capture -> data/capture.pcap, data/pod_ips.txt
# tab 3: generate load from pods INSIDE the cluster, so the logs show pod IPs:
kubectl run loadgen-normal --image=busybox:1.28 --restart=Never -- /bin/sh -c 'while true; do wget -qO- "http://hpa-app/work?ms=20" >/dev/null; sleep 0.2; done'
kubectl run attacker --image=busybox:1.28 --restart=Never -- /bin/sh -c 'while true; do wget -qO- "http://hpa-app/work?ms=200" >/dev/null; done'
kubectl delete pod loadgen-normal attacker     # when the run is over
```

Parse:

```bash
python3 parse_access_logs.py data/logs --ip-map data/pod_ips.txt --metrics data/anomaly_run.csv --out data/anomaly_run_with_logs.csv --pretty 10
python3 pcap_to_flows.py data/capture.pcap --pod-map data/pod_ips.txt --server-match hpa-app --metrics data/anomaly_run.csv --merged-out data/anomaly_run_with_network.csv
```

Outputs: `data/requests.csv`, `data/flows.csv`, per-interval feature CSVs and the merged files.

Caveats:
- `kubectl port-forward` traffic always shows 127.0.0.1 and never reaches the node capture. Use in-cluster load pods.
- NodePort traffic can show the node IP (source NAT). X-Forwarded-For can be forged.
- A pod's logs vanish when it scales down, so `collect_app_logs.sh` must be running during the test.
- The port 8080 capture also sees other pods (kube-state-metrics); `--server-match hpa-app` drops them.

---

## Stage 7: Labelled experiment runs and evaluation [todo]

Answers the professor's feedback: more data, no train/test leakage, request rate fixed, a reason for
leaving out desired replicas and for the contamination value, and real accuracy numbers.

Files (written 2026-10-18, tested with a fake kubectl, a fake Prometheus and synthetic data, not yet
on the real cluster): `run_scenarios.py`, `export_runs.py`, `evaluate.py`, `simulate_runs.py`.
This replaces the old `workload.py` plan, which was never used.

Before every session: Docker Desktop running, laptop set to not sleep, OneDrive paused (optional),
and the Prometheus port-forward open in its own tab:

```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090
```

7a. Check that every query returns data (request_rate must not be 0 while load is running):

```bash
kubectl run curl-test --rm -i --restart=Never --image=busybox:1.28 -- sh -c 'for i in $(seq 200); do wget -qO- "http://hpa-app/work?ms=20" >/dev/null; done'
python3 export_runs.py --check
```

7b. Pilot (short versions of one normal and one attack scenario, about 25 min with settling):

```bash
python3 run_scenarios.py list
python3 run_scenarios.py run normal_steady attack_flood --scale 0.4 --suffix _pilot
python3 export_runs.py normal_steady_pilot attack_flood_pilot --out data/pilot.csv
```

Check the printed summary: request_rate above 0, CPU in a sensible range (a few hundred % at most;
the old 815% was double counted), replicas going up during the flood.

7c. Full set (about 2.5 h; it waits for the HPA to return to its minimum between runs):

```bash
python3 run_scenarios.py run all
python3 export_runs.py
```

Repeats give more data: `python3 run_scenarios.py run all --suffix _r2` (the split keeps every
repeat of a scenario in the same set). If a run is interrupted, run the same command again: finished
runs are skipped, the interrupted one starts over.

7d. Evaluate:

```bash
python3 evaluate.py
```

Read `results/report.md` (contamination sweep, baselines, feature ablation) and the plots. When
the choices are final, run the test evaluation ONCE:

```bash
python3 evaluate.py --final
```

It writes `results/final_report.md`, `results/test_metrics.csv` and `data/isolation_forest_v2.joblib`,
and logs every test evaluation in `results/test_log.csv` (it warns if the test set was already used).

Practice without the cluster (synthetic data, kept in separate files and marked as synthetic):

```bash
python3 simulate_runs.py --repeats 2
python3 evaluate.py --data data/dataset_sim.csv --out results_sim
```

Scenarios: normal_steady 15 min, normal_ramp 18, normal_wave 20, normal_bursts 14 (three 45 s
benign bursts labelled normal), attack_flood 20 (two 3 min floods of cheap requests), attack_heavy
20 (two 3 min periods of expensive 800 ms requests), attack_yoyo 17 (12 min of 75 s on / 105 s off,
labelled as one attack window). All run on top of background traffic of about 3.6 req/s.

Split (by whole run, `SPLIT` in `evaluate.py`): train normal_steady + normal_ramp; validation
normal_wave + attack_flood; test normal_bursts + attack_heavy + attack_yoyo. The heavy and yo-yo
attacks are never seen while tuning.

How it works / caveats:
- Load pods (`lg-...`, label `role=loadgen`) are created before the run and each waits for its
  exact start time, so labels are exact. Leftovers: `kubectl delete pod -l role=loadgen`.
- Times are in the Docker VM clock (the clock Prometheus uses); the offset to the Mac clock is
  measured each run and saved in `data/runs/<run>/meta.json` with the HPA settings and app image.
- CPU counts only `container="hpa-app"`. Old CPU numbers are about twice too high: do not mix old
  and new data.
- Every metric uses a 1-minute rate window, so it reacts up to a minute after a label changes.
  The scrape interval is 15 s (Stage 4), the same as the export step.
- Normal rows in the 60 s after an attack are not scored as false alarms (`--grace`).
- Few attack episodes per run: repeat runs if time allows.

---

## Useful commands

```bash
kubectl get pods -A                     # everything running
kubectl describe hpa php-apache         # why HPA scaled (events)
kubectl get deployment php-apache
kubectl delete hpa php-apache           # remove autoscaler only
kind get clusters
kind delete cluster --name hpa-lab      # wipe the whole cluster and start over
```

---

## Errors seen so far

| Error | Cause | Fix |
|---|---|---|
| `docker: executable file not found` | Docker not installed | Install Docker Desktop, open it, run `docker info` |
| `localhost:8080 connection refused` (kubectl) | No cluster existed | Create the cluster first with `kind create cluster` |
| `zsh: command not found: #` | zsh does not accept pasted comments | Paste commands only, or `setopt interactive_comments` |
| `--cpu-percent has been deprecated` | Old flag | Use `--cpu=50%` |
| `request_rate` always 0 | Old `/metrics` sent `text/html`, Prometheus rejected it | App v3 (`content_type=CONTENT_TYPE_LATEST`) |
| CPU up to 815% | Query summed the container and the pod-level series | Filter `container="hpa-app"` (export_runs.py) |