import pandas as pd
from sklearn.ensemble import IsolationForest
import matplotlib.pyplot as plt
import joblib

# 1. Load the dataset
df = pd.read_csv('data/anomaly_run.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
df.sort_values('timestamp', inplace=True)

# 2. Select features
X = df[['replicas', 'cpu_utilization', 'request_rate']].fillna(0)

# 3. Train the model
model = IsolationForest(contamination=0.15, random_state=42)
df['anomaly_label'] = model.fit_predict(X)
df['is_anomaly'] = df['anomaly_label'] == -1

# 4. Save the trained model for the live detector
joblib.dump(model, 'data/isolation_forest.joblib')
print("Model saved to data/isolation_forest.joblib")

# 5. Visualize the results
plt.figure(figsize=(12, 6))
plt.plot(df['timestamp'], df['cpu_utilization'], label='CPU Utilization (%)', color='blue', alpha=0.5)
plt.plot(df['timestamp'], df['replicas'] * 10, label='Replicas (x10)', color='green', linewidth=2)

anomalies = df[df['is_anomaly']]
plt.scatter(anomalies['timestamp'], anomalies['cpu_utilization'], color='red', s=50, label='Detected Anomaly', zorder=5)

plt.title('Kubernetes HPA Anomaly Detection (Isolation Forest)')
plt.xlabel('Time')
plt.ylabel('Metric Value')
plt.legend()
plt.grid(True, linestyle='--', alpha=0.7)
plt.tight_layout()

plt.savefig('data/anomaly_results.png')
print(f"Model training complete! Detected {len(anomalies)} anomalous data points.")
print("Results graph saved to data/anomaly_results.png")
