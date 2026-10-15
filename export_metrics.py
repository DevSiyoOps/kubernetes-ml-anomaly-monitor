import requests
import csv
import time

PROMETHEUS_URL = "http://localhost:9090/api/v1/query_range"

# time.time() returns the exact UTC Unix epoch, preventing timezone drift
end_timestamp = time.time()
start_timestamp = end_timestamp - (45 * 60) # Look back 45 minutes

queries = {
    "replicas": 'sum(kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="hpa-app"})',
    "desired_replicas": 'sum(kube_horizontalpodautoscaler_status_desired_replicas{horizontalpodautoscaler="hpa-app"})',
    "cpu_utilization": 'sum(rate(container_cpu_usage_seconds_total{namespace="default", pod=~"hpa-app.*"}[1m])) * 100',
    "request_rate": 'sum(rate(app_requests_total[1m]))'
}

data_matrix = {}

for metric_name, query in queries.items():
    try:
        response = requests.get(PROMETHEUS_URL, params={
            'query': query,
            'start': start_timestamp,
            'end': end_timestamp,
            'step': '15s'
        }).json()
        
        if response.get('status') == 'success' and response['data']['result']:
            for value in response['data']['result'][0]['values']:
                ts, val = value
                if ts not in data_matrix:
                    data_matrix[ts] = {}
                data_matrix[ts][metric_name] = float(val)
    except Exception as e:
        print(f"Warning: Failed to fetch {metric_name} - {e}")

with open('data/anomaly_run.csv', 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['timestamp', 'replicas', 'desired_replicas', 'cpu_utilization', 'request_rate'])
    for ts in sorted(data_matrix.keys()):
        row = data_matrix[ts]
        writer.writerow([
            ts, 
            row.get('replicas', 0), 
            row.get('desired_replicas', 0), 
            row.get('cpu_utilization', 0), 
            row.get('request_rate', 0)
        ])

print("Data exported successfully to data/anomaly_run.csv")
