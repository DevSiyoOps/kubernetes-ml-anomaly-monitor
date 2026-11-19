# CLAUDE.md

Guide for anyone (person or Claude session) analysing this project. Read this first, then
`HANDOFF.md` for status and open items. Facts here were checked against the code and result files
on 2026-11-19 (commit `a5552d1`).

## What the project is

SENG 5070 individual project. Question: when the Kubernetes Horizontal Pod Autoscaler (HPA) adds
pods, was it real traffic or an attack? An **Isolation Forest** (unsupervised, scikit-learn) learns
normal cluster behaviour from Prometheus metrics and flags 15-second snapshots that do not fit.
Labels exist only to grade the model; it never trains on them.

## Ground rules (do not break these)

1. **The test set is scored once.** Never choose features, contamination, grace period or anything
   else by looking at test results. `evaluate.py --final` appends to `3_model_output/test_log.csv`
   and warns when the test set has already been scored. It already has 2 entries: the second
   repeated the first with the same frozen settings, after the baselines were removed, and gave the
   same F1. Running `--final` again counts as another use of the test set, and that includes
   running it only to rebuild the `.joblib` model.
2. **Use the terms training / validation / test set**, not learn / practice / exam.
   (`README.md` still says learn / practice / exam in places. That's a known inconsistency.)
3. **Don't commit or push** unless Siya says so. Commits go as DevSiyoOps with the noreply
   email and no Claude co-author line.
4. Do analysis on copies or in the scratchpad. Don't overwrite files in `2.5_hour_run_results/`.
   They are the only copy of that data, because Prometheus keeps 10 days and those runs were
   recorded on Nov 4–5.

## Repository map

| Path | What it is |
|---|---|
| `app/` | Flask app (`app.py`, `Dockerfile`, `requirements.txt`). `/work?ms=N` burns N ms CPU (max 5000); `/metrics` exposes `app_requests_total`, `app_request_duration_seconds`, `app_inflight_requests` |
| `k8s/hpa-app.yaml` | ServiceMonitor only. The Deployment and HPA were created with `kubectl` and are not in a file |
| `run_scenarios.py` | Generates labelled traffic (busybox pods running `wget` loops) and writes `labels.csv` + `meta.json` per run |
| `export_runs.py` | Pulls Prometheus metrics for each run → `raw_runs/<run>/prometheus_log.csv` + `2_model_input/dataset.csv` |
| `evaluate.py` | The ML pipeline: split, scale, train, tune on validation, `--final` scores the test set |
| `plot_contamination.py` | Draws `contamination_explained.png` (alarm line on real training and test scores) |
| `live_detector.py` | Live check every 15 s. **Stale:** loads the Checkpoint 1 three-feature model from `old/data/` |
| `progress_check2.ipynb` | Progress Check 2 notebook (pilot results; wording updated, not re-run) |
| `2.5_hour_run_results/` | The finished experiment (see below) |
| `7.5_hour_run_results/` | Default output for the planned repeat runs. Doesn't exist yet |
| `old/` | Checkpoint 1 and pilot scripts/data, kept for reference only. Don't use for results |
| `HANDOFF.md` | Status, environment, deliverables, ordered open items |

`2.5_hour_run_results/` layout:
- `1_prometheus_metrics/`: 7 readable per-run CSVs (`1_normal_constant_traffic.csv` …
  `7_attack_yoyo.csv`) and `raw_runs/<run>_full/{labels.csv, meta.json, prometheus_log.csv}`
- `2_model_input/`: `dataset.csv` (all runs, all columns) and `model_input_7_features.csv`
  (the 7 model features + `split` + `label`)
- `3_model_output/`: `report.md` (validation tuning), `final_report.md` (test), `selected.json`
  (frozen settings), metric CSVs, `predictions.csv`, plots
- `model/isolation_forest.joblib`: **not in git** on purpose. It exists only on Siya's machine.

## Running things

```bash
pip3 install pandas matplotlib scikit-learn joblib requests   # tested: Python 3.14, sklearn 1.9, pandas 3.0

# Offline analysis of the finished experiment (no cluster needed):
RESULTS_ROOT=2.5_hour_run_results python3 evaluate.py    # validation tuning only; safe to re-run
python3 plot_contamination.py

# Needs the cluster (Docker Desktop → kind cluster hpa-lab) and port-forwards:
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
kubectl port-forward svc/hpa-app 8080:80
python3 run_scenarios.py list
python3 run_scenarios.py run all --suffix _r2            # ~2.5 h per full set
python3 export_runs.py
```

All scripts read `RESULTS_ROOT` (default `7.5_hour_run_results`). Set
`RESULTS_ROOT=2.5_hour_run_results` to work on the finished data. Note that re-running
`evaluate.py` without `--final` rewrites `report.md`, `selected.json` and the validation files in
`3_model_output/`. The results come out the same because the seeds are fixed, but point `--out`
at a scratch folder if you are experimenting.

There are no tests and no linter config.

## Data

**Traffic shapes** (`run_scenarios.py`, constants `BASE`, `BENIGN`, `FLOOD`, `HEAVY`, `YOYO_ON`):

| Load | wget loops | ms per request | sleep | Label |
|---|---|---|---|---|
| Background | 2 | 40 | 0.5 s | normal (always on, ~3.6 req/s) |
| Busy burst | 4 | 40 | 0.2 s | normal (flash crowd) |
| Flood | 16 | 10 | 0 | attack |
| Heavy | 3 | 800 | 0.2 s | attack |
| Yo-yo | 10 | 40 | 0 | attack (75 s on / 105 s off, ×4) |

**Labels:** `labels.csv` holds each segment's start and end (Unix seconds, Docker VM clock, the same
clock as Prometheus; the offset from the Mac clock is in `meta.json`). `export_runs.py` sets
`label = 1` for rows inside an attack window. Every PromQL query uses a 1-minute `rate()` window,
so the metrics lag the labels by up to a minute. Rows with `in_run = 0` are the minute exported
before the run starts and are dropped by `evaluate.py`.

**Split is by scenario, never by row** (`SPLIT` in `evaluate.py`). Rows 15 s apart are near
duplicates, so a random row split would leak.

| Set | Runs | Rows | Attack rows |
|---|---|---|---|
| Training | normal_steady, normal_ramp | 134 | 0 |
| Validation | normal_wave, attack_flood | 162 | 24 |
| Test | normal_bursts, attack_heavy, attack_yoyo | 207 | 65 |

Total 503 in-run rows, 89 attack rows, all at a 15 s step. Some consequences:
- The **test attacks (heavy, yo-yo) are attack types the model was never tuned on**. Validation
  only contains the flood attack. So the test measures generalisation to new attack types, not
  just new data.
- Repeat runs (`_r2` …) map to the same set as their scenario automatically, because `SPLIT` keys
  on `scenario`, not `run`.
- `--final` refits on training + the all-normal validation runs (215 rows). Normal rows within
  60 s after an attack ends are not scored (`--grace`), so the test set scores 130 normal + 65
  attack rows.

**Units gotcha:** `cpu_utilization` is the total CPU of all pods as % of **one core** (34.5 = 345m).
The HPA's 50% target is average per pod against a 50m request. Don't mix data from `old/`: the old
export also counted pod-level series, which doubled CPU.

**Export step:** `export_runs.py --step` now defaults to 5 s, but Prometheus scrapes every 15 s,
so 5 s rows repeat values. The finished dataset is at 15 s.

## Model

- Features (set `B_extended`, 7): `replicas`, `cpu_utilization`, `request_rate` (from Prometheus)
  + `cpu_per_request`, `latency_p95_s`, `replica_changes_5m`, `cpu_std_5m` (derived in
  `add_features`; rolling windows are 5 min per run).
- Pipeline: `StandardScaler` → `IsolationForest(n_estimators=200)`, fitted on normal training rows
  only. Results are the mean over seeds 0–4.
- Contamination swept over `[0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2]` on validation.
  `pick()` takes the highest mean F1, then the lowest false alarm rate, then the smaller
  contamination. Chosen: **0.03**. The sweep runs only on the `--features` set (default B). The
  other sets are compared afterwards in `ablation.csv`, each at its own best contamination.
- Score: `-score_samples` (higher = weirder). The alarm threshold is the training-score percentile
  at the contamination level.

## Results (from `3_model_output/`)

| Set | Precision | Recall | F1 | FPR | ROC AUC | Episodes caught | Latency |
|---|---|---|---|---|---|---|---|
| Validation (B, c=0.03) | 0.85 | 0.92 | 0.88 | 3.1% | 0.91 | 2 / 2 | 15 s |
| **Test (scored once)** | 0.78 | 0.77 | 0.78 | 10.6% | 0.85 | 3 / 3 | 15 s |

Per test run: heavy F1 0.91 · yo-yo F1 0.78 (row recall 0.68, because the quiet halves of the
yo-yo window are labelled attack) · busy bursts (normal) 16% of rows flagged.

## Things an analyst should know (caveats, verified)

- **Feature set A (3 features) beat the chosen set B on validation**: F1 0.93 at c=0.005 vs 0.88
  (`ablation.csv`). B is the default in code ("the model we propose"), not the validation winner.
  The comments in `add_features` say why the extras exist: `cpu_per_request` is high for expensive
  requests (heavy attack) and `replica_changes_5m` captures oscillation (yo-yo). Validation has
  neither attack type, so it can't reward those features. HANDOFF item 5
  plans to re-check A vs B on the repeat runs. Don't settle it on the test set.
- **The removed CPU-threshold baseline scored F1 0.79 on the test set**, slightly above the
  Isolation Forest's 0.78 (first row of `test_log.csv`; the z-score baseline got 0.40). The
  baselines were removed from the code in `b1a1510`. Keep this in mind when claiming the model
  beats simple rules.
- **Half the test false alarms are run-start rows.** With seed 0, 7 of the 14 scored false alarms
  are the first 2–3 rows of a test run (1 pod, near-zero CPU and request rate while traffic
  starts). The rest are mostly the busy-burst flash crowd. Fixing this (e.g. a warm-up trim)
  must be decided on validation or repeat runs, not on these test numbers.
- **Small samples:** validation has 2 attack episodes and the test set has 3. Episode recall and
  latency (always 15 s, one scrape) are coarse. Validation F1 has zero spread across seeds.
- **Pod count barely separates normal from attack:** with a 50m request, normal traffic already
  reaches ~6–10 pods. HANDOFF item 6 proposes 100–200m.
- `selected.json` still says `"data": "data/dataset.csv"`, the path from before the folder move.
  It's informational only; `--final` uses `--data`.

## Status

Finished experiment and test scoring are committed. Next steps (details in `HANDOFF.md` §8):
3 repeat runs (`_r2`–`_r4`, ~7.5 h) → re-tune on the larger validation set → score the test set
once more and report it as a re-run → bigger pod CPU request → update `live_detector.py` to the
7-feature model → final report.
