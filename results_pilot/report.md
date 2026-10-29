# Validation report (steps 1-5)

Data: `data/dataset.csv`, rows inside runs only, one row per 15 s. Normal rows in the 60 s after an attack are not scored.

## Split (by whole run)

| split | run | rows | attack rows |
|---|---|---|---|
| train | normal_steady | 25 | 0 |
| validation | attack_flood | 33 | 10 |


Training uses only the normal rows of the train runs (25 rows). The scaler and the model are fitted on those rows only. The test runs are not used in this report.

## Contamination sweep (B_extended, validation, mean of 5 seeds)

With normal-only training, contamination is the share of normal training rows the model may call anomalous, so it sets the alarm threshold. It was chosen by the best mean F1 on validation (ties: lower false-positive rate).

| contamination | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| 0.005 | 0.000 | 0.000 | 0.000 | 0.000 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.01 | 0.000 | 0.000 | 0.000 | 0.067 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.02 | 0.000 | 0.000 | 0.000 | 0.067 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.03 | 0.000 | 0.000 | 0.000 | 0.067 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.05 | 0.000 | 0.000 | 0.000 | 0.067 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.075 | 0.000 | 0.000 | 0.000 | 0.120 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.1 | 0.000 | 0.000 | 0.000 | 0.120 | 0.667 | 0.551 | 0.0/2 | n/a |
| 0.15 | 0.180 | 0.080 | 0.110 | 0.173 | 0.667 | 0.551 | 0.4/2 | 15 |
| 0.2 | 0.479 | 0.520 | 0.493 | 0.267 | 0.667 | 0.551 | 1.4/2 | 16 |


Chosen: **0.2**. Plot: `contamination_sweep.png`.

Confusion matrices, ROC and precision-recall curves on validation (Isolation Forest seed 0): `validation_confusion_matrix.png`, `validation_roc_curve.png`, `validation_pr_curve.png`.

## Detectors on validation (each tuned on these runs)

| detector | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| Isolation Forest (B_extended, c=0.2) | 0.479 | 0.520 | 0.493 | 0.267 | 0.667 | 0.551 | 1.4/2 | 16 |
| CPU threshold (> 15.7%) | 1.000 | 0.900 | 0.947 | 0.000 | 0.967 | 0.967 | 2.0/2 | 9 |
| rolling z-score (window 8, k=2.25) | 1.000 | 0.700 | 0.824 | 0.000 | 0.893 | 0.891 | 2.0/2 | 24 |


## Per workload (each validation run)

FPR on a normal run = share of its rows flagged by mistake. Recall and episodes only apply to attack runs.

| run | detector | FPR | recall | episodes found | mean latency s |
|---|---|---|---|---|---|
| attack_flood | Isolation Forest | 0.267 | 0.520 | 1.4/2 | 16 |
| attack_flood | CPU threshold | 0.000 | 0.900 | 2.0/2 | 9 |
| attack_flood | z-score | 0.000 | 0.700 | 2.0/2 | 24 |


## Feature ablation (validation, each set with its own best contamination)

| set | c | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|---|
| A_original | 0.005 | 0.000 | 0.000 | 0.000 | 0.000 | 0.684 | 0.670 | 0.0/2 | n/a |
| B_extended | 0.2 | 0.479 | 0.520 | 0.493 | 0.267 | 0.667 | 0.551 | 1.4/2 | 16 |
| C_plus_desired | 0.2 | 0.380 | 0.380 | 0.369 | 0.267 | 0.629 | 0.515 | 1.2/2 | 20 |
| D_plus_network | 0.2 | 0.319 | 0.340 | 0.320 | 0.227 | 0.673 | 0.555 | 1.0/2 | 21 |


Sets: **A_original** = replicas, cpu_utilization, request_rate; **B_extended** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m; **C_plus_desired** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m, desired_replicas; **D_plus_network** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m, net_rx_kbps, net_tx_kbps, memory_mb.


`desired_replicas` (set C) is the HPA's own reaction to CPU, so it repeats a signal the model already has and changes whenever the HPA settings change. Compare C with B: if C is not clearly better, leaving it out costs nothing and keeps the model independent of the HPA configuration.


Next: when these choices are final, run `python3 evaluate.py --final` once.
