# Kubernetes HPA Anomaly Detection

When the Kubernetes autoscaler (HPA) adds pods, was it real traffic or an attack?
An Isolation Forest learns what normal cluster behaviour looks like from Prometheus metrics and
flags snapshots that do not fit.

## Folders (in pipeline order)

```text
MLproject/
├── app/                     # the Flask app: /work?ms=N burns N ms of CPU, /metrics for Prometheus
├── k8s/hpa-app.yaml         # ServiceMonitor so Prometheus scrapes the app
│
├── 2.5_hour_run_results/        # the finished 2.5-hour run (7 tests, exam scored once)
│   ├── 1_prometheus_metrics/    # STEP 1: what Prometheus recorded, one row every 15 s
│   │   ├── 1_normal_constant_traffic.csv
│   │   ├── 2_normal_rising_traffic.csv
│   │   ├── 3_normal_up_down_traffic.csv
│   │   ├── 4_attack_many_cheap_requests.csv
│   │   ├── 5_normal_busy_bursts.csv
│   │   ├── 6_attack_few_expensive_requests.csv
│   │   ├── 7_attack_yoyo.csv
│   │   └── raw_runs/            # per test: labels.csv (when each attack ran), meta.json, prometheus_log.csv
│   ├── 2_model_input/           # STEP 2: what the model is fed
│   │   ├── dataset.csv                  # all 7 tests together
│   │   └── model_input_7_features.csv   # exactly the 7 numbers the model sees + the label
│   ├── 3_model_output/          # STEP 3: what the model produced
│   │   ├── predictions.csv      # every exam snapshot: actual vs model_says (attack/normal) + oddness score
│   │   ├── test_metrics.csv     # final score (precision, recall, F1, false alarms, time to catch)
│   │   ├── final_report.md      # exam results
│   │   ├── report.md            # tuning on the practice data
│   │   └── *.png                # timelines, confusion matrices, contamination sweep
│   └── model/isolation_forest.joblib   # the trained model
│
├── 7.5_hour_run_results/        # the repeat runs go here (same layout), created on the first run
└── old/                         # Checkpoint 1 and pilot files, kept for reference
```

New runs go to `7.5_hour_run_results/`. To work on the finished set instead:
`RESULTS_ROOT=2.5_hour_run_results python3 evaluate.py`

## Scripts

| Script | What it does |
|---|---|
| `run_scenarios.py` | Sends timed traffic (busybox pods running `wget` loops) and writes when each attack ran |
| `export_runs.py` | Pulls the metrics from Prometheus for each test and builds `2_model_input/dataset.csv` |
| `evaluate.py` | Splits by test (learn / practice / exam), tunes on practice, scores the exam once |
| `plot_contamination.py` | Draws `contamination_explained.png`: the alarm line on the real training and test scores |
| `live_detector.py` | Checks the live cluster every 15 s and prints "System Normal" or "ANOMALY DETECTED" |

## The 7 features

pods, CPU, requests per second, CPU per request, slowness (p95 latency),
pod changes in the last 5 min, CPU wobble in the last 5 min.

## Requirements

```bash
pip3 install pandas matplotlib scikit-learn joblib requests
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
```
