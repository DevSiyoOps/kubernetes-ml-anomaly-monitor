# Validation report (steps 1-5)

Data: `data/dataset.csv`, rows inside runs only, one row per 15 s. Normal rows in the 60 s after an attack are not scored.

## Split (by whole run)

| split | run | rows | attack rows |
|---|---|---|---|
| train | normal_ramp_full | 73 | 0 |
| train | normal_steady_full | 61 | 0 |
| validation | attack_flood_full | 81 | 24 |
| validation | normal_wave_full | 81 | 0 |
| test | attack_heavy_full | 81 | 24 |
| test | attack_yoyo_full | 69 | 41 |
| test | normal_bursts_full | 57 | 0 |


Training uses only the normal rows of the train runs (134 rows). The scaler and the model are fitted on those rows only. The test runs are not used in this report.

## Contamination sweep (B_extended, validation, mean of 5 seeds)

With normal-only training, contamination is the share of normal training rows the model may call anomalous, so it sets the alarm threshold. It was chosen by the best mean F1 on validation (ties: lower false-positive rate).

| contamination | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| 0.005 | 0.000 | 0.000 | 0.000 | 0.003 | 0.912 | 0.715 | 0.0/2 | n/a |
| 0.01 | 0.000 | 0.000 | 0.000 | 0.015 | 0.912 | 0.715 | 0.0/2 | n/a |
| 0.02 | 0.508 | 0.550 | 0.528 | 0.028 | 0.912 | 0.715 | 1.2/2 | 15 |
| 0.03 | 0.846 | 0.917 | 0.880 | 0.031 | 0.912 | 0.715 | 2.0/2 | 15 |
| 0.05 | 0.792 | 0.917 | 0.849 | 0.045 | 0.912 | 0.715 | 2.0/2 | 15 |
| 0.075 | 0.623 | 0.917 | 0.741 | 0.103 | 0.912 | 0.715 | 2.0/2 | 15 |
| 0.1 | 0.548 | 0.917 | 0.686 | 0.140 | 0.912 | 0.715 | 2.0/2 | 15 |
| 0.15 | 0.472 | 0.917 | 0.623 | 0.189 | 0.912 | 0.715 | 2.0/2 | 15 |
| 0.2 | 0.416 | 0.917 | 0.572 | 0.240 | 0.912 | 0.715 | 2.0/2 | 15 |


Chosen: **0.03**. Plot: `contamination_sweep.png`.

Confusion matrices, ROC and precision-recall curves on validation (Isolation Forest seed 0): `validation_confusion_matrix.png`, `validation_roc_curve.png`, `validation_pr_curve.png`.

## Detectors on validation (each tuned on these runs)

| detector | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| Isolation Forest (B_extended, c=0.03) | 0.846 | 0.917 | 0.880 | 0.031 | 0.912 | 0.715 | 2.0/2 | 15 |
| CPU threshold (> 34.5%) | 1.000 | 0.917 | 0.957 | 0.000 | 0.963 | 0.941 | 2.0/2 | 15 |
| rolling z-score (window 20, k=2.0) | 0.684 | 0.542 | 0.605 | 0.046 | 0.890 | 0.707 | 2.0/2 | 22 |


## Per workload (each validation run)

FPR on a normal run = share of its rows flagged by mistake. Recall and episodes only apply to attack runs.

| run | detector | FPR | recall | episodes found | mean latency s |
|---|---|---|---|---|---|
| attack_flood_full | Isolation Forest | 0.041 | 0.917 | 2.0/2 | 15 |
| attack_flood_full | CPU threshold | 0.000 | 0.917 | 2.0/2 | 15 |
| attack_flood_full | z-score | 0.000 | 0.542 | 2.0/2 | 22 |
| normal_wave_full | Isolation Forest | 0.025 | n/a | - | n/a |
| normal_wave_full | CPU threshold | 0.000 | n/a | - | n/a |
| normal_wave_full | z-score | 0.074 | n/a | - | n/a |


## Feature ablation (validation, each set with its own best contamination)

| set | c | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|---|
| A_original | 0.005 | 0.935 | 0.917 | 0.925 | 0.012 | 0.944 | 0.935 | 2.0/2 | 15 |
| B_extended | 0.03 | 0.846 | 0.917 | 0.880 | 0.031 | 0.912 | 0.715 | 2.0/2 | 15 |
| C_plus_desired | 0.05 | 0.754 | 0.917 | 0.827 | 0.055 | 0.915 | 0.677 | 2.0/2 | 15 |
| D_plus_network | 0.03 | 0.834 | 0.917 | 0.873 | 0.034 | 0.916 | 0.731 | 2.0/2 | 15 |


Sets: **A_original** = replicas, cpu_utilization, request_rate; **B_extended** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m; **C_plus_desired** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m, desired_replicas; **D_plus_network** = replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m, net_rx_kbps, net_tx_kbps, memory_mb.


`desired_replicas` (set C) is the HPA's own reaction to CPU, so it repeats a signal the model already has and changes whenever the HPA settings change. Compare C with B: if C is not clearly better, leaving it out costs nothing and keeps the model independent of the HPA configuration.


Next: when these choices are final, run `python3 evaluate.py --final` once.
