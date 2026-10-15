# Kubernetes HPA Monitoring and Anomaly Detection

This project collects Kubernetes Horizontal Pod Autoscaler (HPA) metrics from Prometheus, stores them in CSV files, visualizes the trends, and trains an unsupervised anomaly-detection model to identify unusual scaling behavior.

The work is focused on monitoring a custom `hpa-app` deployment in a local Kubernetes environment to detect abnormal autoscaling events (such as malicious load spikes) by analyzing CPU utilization, request rates, and replica counts over time.

## Project Purpose

This repository contains a complete end-to-end AIOps pipeline for:
- Deploying a target application and Prometheus ServiceMonitor.
- Exporting live Prometheus telemetry into structured datasets.
- Training a machine-learning Isolation Forest model offline.
- Running a live inference watchdog to trigger real-time terminal alerts.

## Folder Contents

```text
MLproject/
├── app/                      # Target application source code and Dockerfile
├── k8s/                      # Kubernetes deployment and ServiceMonitor manifests
│   └── hpa-app.yaml          
├── export_metrics.py         # Exports Prometheus HPA metrics to CSV
├── load_and_plot.py          # Loads a CSV and generates basic metric plots
├── train_model.py            # Trains an Isolation Forest anomaly model
├── live_detector.py          # Real-time inference engine and alerting watchdog
└── data/
    ├── run1.csv              # Initial test dataset
    ├── anomaly_run.csv       # Historical attack dataset used for training
    ├── isolation_forest.joblib # Saved trained model state
    └── anomaly_results.png   # Plotted graph of detected anomalies
```

## Scripts & Workflow

### 1) Infrastructure & Load Generation
The target application and Prometheus ServiceMonitor are deployed via `k8s/hpa-app.yaml`. A simulated attack is generated using a heavy load container to trigger the HPA scale-up.

### 2) `export_metrics.py`
This script queries Prometheus for key HPA and container metrics over a 45-minute lookback window and writes them to `data/anomaly_run.csv`. 

It extracts:
- `replicas`
- `desired_replicas`
- `cpu_utilization`
- `request_rate`

*Note: It expects a port-forwarded Prometheus connection:*
```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
```

### 3) `train_model.py`
This script trains an unsupervised `IsolationForest` model on the historical CSV metrics to detect abnormal autoscaling behavior. 

It executes the following:
- Ingests `replicas`, `cpu_utilization`, and `request_rate`.
- Labels multidimensional anomalies.
- Saves the trained model to `data/isolation_forest.joblib`.
- Generates a visual proof-of-detection graph (`data/anomaly_results.png`).

### 4) `live_detector.py`
The final inference engine. This script runs continuously, loading the `.joblib` model into memory and polling Prometheus every 15 seconds. It evaluates current cluster telemetry against the trained model to print real-time "System Normal" checks or "ANOMALY DETECTED" alerts.

## Requirements

Install the required Python packages with:

```bash
pip3 install pandas matplotlib scikit-learn joblib requests
```

## Notes
- This project is developed as an experimental ML pipeline for Kubernetes observability.
- The Isolation Forest model is unsupervised, making it ideal for identifying out-of-pattern system behavior without requiring pre-labeled attack data.
- Metric queries are specifically tuned for the `hpa-app` namespace and workload selectors.