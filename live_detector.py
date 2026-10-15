import requests
import time
import joblib
import pandas as pd
from datetime import datetime

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
MODEL_PATH = "data/isolation_forest.joblib"

print(f"Loading trained model from {MODEL_PATH}...")
model = joblib.load(MODEL_PATH)

queries = {
    "replicas": 'sum(kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler="hpa-app"})',
    "cpu_utilization": 'sum(rate(container_cpu_usage_seconds_total{namespace="default", pod=~"hpa-app.*"}[1m])) * 100',
    "request_rate": 'sum(rate(app_requests_total[1m]))'
}

print("Starting live anomaly detection...")

while True:
    current_data = {}
    try:
        for name, query in queries.items():
            resp = requests.get(PROMETHEUS_URL, params={'query': query}).json()
            results = resp.get('data', {}).get('result', [])
            current_data[name] = float(results[0]['value'][1]) if results else 0.0
        
        # Format for the model
        df_live = pd.DataFrame([current_data])[['replicas', 'cpu_utilization', 'request_rate']].fillna(0)
        
        # Predict (1 = Normal, -1 = Anomaly)
        prediction = model.predict(df_live)[0]
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        metrics_str = f"CPU: {current_data['cpu_utilization']:>6.1f}% | Replicas: {current_data['replicas']:>2.0f} | Req/s: {current_data['request_rate']:>4.1f}"
        
        if prediction == -1:
            print(f"🚨 [{timestamp}] ANOMALY DETECTED! | {metrics_str}")
        else:
            print(f"✅ [{timestamp}] System Normal.    | {metrics_str}")
            
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connection error: {e}")
        
    time.sleep(15) # Wait for the next Prometheus scrape interval
