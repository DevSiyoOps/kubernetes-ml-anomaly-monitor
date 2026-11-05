# Test results (steps 6-7, evaluated once)

Settings fixed on validation (2026-11-05 15:34): features B_extended (replicas, cpu_utilization, request_rate, cpu_per_request, latency_p95_s, replica_changes_5m, cpu_std_5m), contamination 0.03, grace 60 s.

Isolation Forest refitted on 215 normal rows: normal_ramp_full, normal_steady_full, normal_wave_full.

## Test split

| split | run | rows | attack rows |
|---|---|---|---|
| test | attack_heavy_full | 81 | 24 |
| test | attack_yoyo_full | 69 | 41 |
| test | normal_bursts_full | 57 | 0 |


## All test runs

| detector | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| Isolation Forest | 0.784 | 0.769 | 0.776 | 0.106 | 0.845 | 0.770 | 3.0/3 | 15 |


Isolation Forest: mean of 5 seeds (std: precision ±0.009, recall ±0.000, f1 ±0.005, fpr ±0.006).

Confusion matrices, ROC and precision-recall curves (Isolation Forest seed 0): `test_confusion_matrix.png`, `test_roc_curve.png`, `test_pr_curve.png`.


## Per workload (each test run)

FPR on a normal run = share of its rows flagged by mistake. Recall and episodes only apply to attack runs.

| run | detector | FPR | recall | episodes found | mean latency s |
|---|---|---|---|---|---|
| attack_heavy_full | Isolation Forest | 0.045 | 0.917 | 2.0/2 | 15 |
| attack_yoyo_full | Isolation Forest | 0.108 | 0.683 | 1.0/1 | 15 |
| normal_bursts_full | Isolation Forest | 0.158 | n/a | - | n/a |


## Per attack type (normal test rows + that attack's rows)

| detector | precision | recall | F1 | FPR | ROC AUC | avg precision | episodes found | mean latency s |
|---|---|---|---|---|---|---|---|---|
| Isolation Forest, heavy only | 0.615 | 0.917 | 0.736 | 0.106 | 0.944 | 0.704 | 2.0/2 | 15 |
| Isolation Forest, yoyo only | 0.670 | 0.683 | 0.676 | 0.106 | 0.787 | 0.661 | 1.0/1 | 15 |


The yo-yo episode is labelled as one window including its quiet halves, so its row recall understates detection; episode recall is the fairer figure. Episode counts are small.
