# Project handoff: Kubernetes HPA anomaly detection (SENG 5070)

As of 2026-11-17 · Siya Grover (individual project)

## 1. Summary and status

**Question:** when the Kubernetes autoscaler (HPA) adds pods, was it real traffic or an attack?

**Method:** an Isolation Forest (unsupervised anomaly detection, scikit-learn) learns what normal
cluster behaviour looks like from Prometheus metrics, then flags 15-second snapshots that do not fit.

**Result on the test set (scored once):** all 3 attacks caught, each within about 15 s.
Precision 0.78, recall 0.77, F1 0.78, false alarm rate 11%.

| Done | Not done |
|---|---|
| Labelled 2.5-hour experiment (7 runs, 503 rows) | 3 repeat runs (about 7.5 h) — set up, not started |
| Train / tune / test pipeline, test set scored | Folder cleanup and recent edits not committed |
| Slides (40) and progress report v3 | `live_detector.py` still uses the old Checkpoint 1 model |
| Baselines (CPU rule, z-score) removed everywhere | Final report |

Repo: github.com/DevSiyoOps/kubernetes-ml-anomaly-monitor, branch `1st-PR`, PR #1.
Last commit: `b1a1510` (baselines removed).

## 2. Environment and how to run

**Cluster:** kind cluster `hpa-lab` (needs Docker Desktop running; the cluster comes back by itself when Docker starts).

| Part | Setting |
|---|---|
| App | Deployment `hpa-app`, Flask, image `hpa-app:v3`, port 8080, Service `hpa-app` port 80 |
| Pod CPU | request 50m (5% of one core), no CPU limit |
| HPA | 1 to 10 pods, target 50% average CPU (= 25m per pod), Kubernetes default timing |
| HPA timing | checks every 15 s; scales up fast; waits 5 min of low CPU before scaling down; 10% tolerance (acts above about 55%) |
| Monitoring | kube-prometheus-stack in namespace `monitoring`, scrape every 15 s, retention 10 days |

**App endpoints:** `/work?ms=N` burns N ms of CPU (max 5000) · `/metrics` gives `app_requests_total`,
`app_request_duration_seconds` (latency), `app_inflight_requests` · every request also logs one JSON line (`kubectl logs`).

**Port-forwards (each in its own terminal):**
```bash
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl port-forward svc/hpa-app 8080:80
```

**Commands:**
```bash
python3 run_scenarios.py list                          # show the 7 scenarios
python3 run_scenarios.py run all --suffix _r2          # run all 7 tests (about 2.5 h)
python3 export_runs.py                                 # pull metrics from Prometheus, build dataset.csv
RESULTS_ROOT=2.5_hour_run_results python3 evaluate.py           # tuning on the validation set
RESULTS_ROOT=2.5_hour_run_results python3 evaluate.py --final   # test set, run ONCE
python3 plot_contamination.py                          # contamination picture
```
Scripts write to `RESULTS_ROOT`, default `7.5_hour_run_results/`. Set it to `2.5_hour_run_results` to work on the finished run.

## 3. Data

**How traffic is made:** `run_scenarios.py` creates one busybox pod per load segment. Each pod waits for its
start time, runs N parallel `wget` loops against `http://hpa-app/work?ms=M`, and stops at its end time.

| Load | Loops | ms per request | Pause | Meaning |
|---|---|---|---|---|
| Normal background | 2 | 40 | 0.5 s | about 3.6 req/s, always on |
| Busy burst (normal) | 4 | 40 | 0.2 s | flash crowd, labelled normal |
| Flood attack | 16 | 10 | none | many cheap requests |
| Heavy attack | 3 | 800 | 0.2 s | few expensive requests |
| Yo-yo attack | 10 | 40 | none | 75 s on, 105 s off, 4 times |

**How labels are made:** the same script writes the start and end of every segment to `labels.csv`
(cluster clock). `export_runs.py` gives each Prometheus row `label = 1` if its timestamp falls inside an
attack window, otherwise 0. The model never sees the labels while training; they are only used to grade it.

**The 7 runs and the split (by whole run, never by row — rows 15 s apart are near-copies, a random split would leak):**

| Run | File in `1_prometheus_metrics/` | Set | Rows | Attack rows |
|---|---|---|---|---|
| normal_steady | 1_normal_constant_traffic.csv | Training | 61 | 0 |
| normal_ramp | 2_normal_rising_traffic.csv | Training | 73 | 0 |
| normal_wave | 3_normal_up_down_traffic.csv | Validation | 81 | 0 |
| attack_flood | 4_attack_many_cheap_requests.csv | Validation | 81 | 24 |
| normal_bursts | 5_normal_busy_bursts.csv | Test | 57 | 0 |
| attack_heavy | 6_attack_few_expensive_requests.csv | Test | 81 | 24 |
| attack_yoyo | 7_attack_yoyo.csv | Test | 69 | 41 |

Total 503 rows, 89 attack rows. Training = 134 normal rows. The final model is retrained on
training + normal validation rows = 215. Normal rows in the 60 s after an attack ends are not scored
(the HPA is still recovering), so the test set scores 130 normal + 65 attack rows.

The first steady run had a 6-minute traffic gap and was re-run.

## 4. Model

**Features (7 per snapshot):** pods (replicas), CPU, requests per second, CPU per request,
p95 latency (slowness), pod changes in the last 5 min, CPU standard deviation in the last 5 min.
The first 3 come from Prometheus; the other 4 are derived in `evaluate.py` (`add_features`).
Left out on purpose: `desired_replicas` (the HPA computes it from CPU; adding it lowered the score).

**Pipeline:** `StandardScaler` (fitted on training rows only) → `IsolationForest`.

| Hyperparameter | Value | How chosen |
|---|---|---|
| contamination | 0.03 | tuned on the validation set |
| n_estimators (trees) | 200 | fixed |
| random_state | seeds 0–4, results averaged | fixed, removes luck |
| feature set | B (7 features) | by design, checked against sets A, C, D |

**Tuning:** 9 contamination values (0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2) × 5 seeds,
trained on the training set, scored on the validation set. Picked by highest mean F1, ties go to lower
false alarm rate. 0.03 gave F1 0.88 with 3% false alarms.

**How it decides:**
- Weirdness score: scikit-learn `score_samples` (sign flipped). Few random cuts to isolate a snapshot → high score.
  Formula in `sklearn/ensemble/_iforest.py`: `score = 2 ** (-depth / (trees × typical depth))`.
- Alarm line: `offset_ = np.percentile(training scores, 100 × contamination)`. With 0.03 the line sits at a
  score of 0.60, so the weirdest 3% of the 215 training snapshots (7) are past it.
- Any new snapshot past the line = attack. See `contamination_explained.png`.

## 5. Results

**Test set (scored once, mean of 5 seeds):**

| Precision | Recall | F1 | False alarms | Attacks caught | Time to catch |
|---|---|---|---|---|---|
| 0.78 | 0.77 | 0.78 | 11% | 3 of 3 | about 15 s |

**Per test run:**

| Run | Result |
|---|---|
| Heavy attack | F1 0.91 |
| Yo-yo attack | F1 0.78; 68% of the attack window flagged (its quiet gaps look normal but are labelled attack) |
| Busy bursts (normal) | 16% of snapshots wrongly flagged |

**Validation set:** F1 0.88, false alarms 3% (pilot was 0.49 and 27%).

**Feature sets (validation F1, each at its own best contamination):** A original 3 = 0.93 · B ours, 7 = 0.88 ·
C = B + desired pods = 0.83 · D = B + network + memory = 0.87.

**Weak spots:** busy normal traffic looks like an attack; the yo-yo gaps; pods are so small (50m) that normal
traffic already uses about 6 of 10 pods, so the pod count barely separates normal from attack.

## 6. Repository

```text
MLproject/
├── app/                    Flask app + Dockerfile
├── k8s/hpa-app.yaml        ServiceMonitor (Deployment and HPA were made with kubectl, not in a file)
├── run_scenarios.py        sends timed traffic, writes labels.csv + meta.json
├── export_runs.py          Prometheus → prometheus_log.csv per run + dataset.csv (default step 5 s)
├── evaluate.py             split, tune on validation, --final scores the test set once
├── plot_contamination.py   draws contamination_explained.png
├── live_detector.py        live check every 15 s (still the old 3-feature model in old/data/)
├── progress_check2.ipynb   Progress Check 2 notebook
├── 2.5_hour_run_results/
│   ├── 1_prometheus_metrics/   7 readable CSVs + raw_runs/<run>/{labels.csv, meta.json, prometheus_log.csv}
│   ├── 2_model_input/          dataset.csv, model_input_7_features.csv
│   ├── 3_model_output/         predictions.csv, test_metrics.csv, final_report.md, report.md, plots
│   └── model/isolation_forest.joblib
├── 7.5_hour_run_results/   created by the first repeat run
└── old/                    Checkpoint 1 and pilot files
```

**Commit state:** everything after `b1a1510` is uncommitted — the folder moves, `prometheus_log.csv`
renames, `predictions.csv`, `plot_contamination.py`, notebook wording, README and this file.
`__pycache__/` and the `.joblib` model are left out on purpose.

## 7. Deliverables

| Item | Where | State |
|---|---|---|
| Slides (40) | Google Slides "Kubernetes HPA Anomaly Detection Pipeline" | Uses training / validation / test throughout; plain-English slides 22–24; page numbers typed in by hand |
| Contamination slide | Slide 27 | Has the first picture; clearer `contamination_explained_v2.png` (in Downloads, also `3_model_output/contamination_explained.png`) ready to swap in |
| Progress report | `~/Downloads/SENG5070_Progress_Report_Siya_Grover_v3.pdf` | Baselines removed; v2 and the original are kept |
| Notebook | `progress_check2.ipynb` | Pilot results; wording updated, not re-run |

## 8. Open items, in order

1. Check the folder cleanup, then commit (as DevSiyoOps, noreply email, no Claude co-author).
2. Swap the clearer contamination picture into slide 27.
3. Start the 3 repeat runs when ready (laptop plugged in, lid open):
   `python3 run_scenarios.py run all --suffix _r2`, then `_r3`, `_r4`; then `python3 export_runs.py`.
4. Re-tune on the bigger validation set, then score the test set once more and report that it was re-run with more data.
5. Re-check feature set A (3 features) vs B on the repeat runs, not on the test set.
6. Give pods a bigger CPU request (100–200m) so the pod count can tell normal from attack.
7. Update `live_detector.py` to the new 7-feature model.
8. Write the final report (due date not verified).

## 9. Rules and gotchas

- **The test set is used once.** Never pick settings on it. `test_log.csv` already shows 2 entries:
  the second was a re-run with identical frozen settings after removing the baselines (same numbers).
- **Terms:** say training / validation / test set, not learn / practice / exam.
- **No commits or pushes** until Siya checks and says so.
- **CPU units differ:** the HPA's 50% is average per pod vs its 50m request; `cpu_utilization` in the data is the
  total CPU of all pods as % of one core (34.5 there = 345m).
- **Clocks:** labels use the Docker VM clock (offset measured per run in `meta.json`), the same clock as Prometheus.
- **5 s export:** Prometheus scrapes every 15 s, so 5 s rows repeat values; only new runs add real data.
- **Prometheus keeps 10 days:** re-exporting old runs works only inside that window.
- **Slide images** need a public URL to be inserted through the API; otherwise upload by hand.
