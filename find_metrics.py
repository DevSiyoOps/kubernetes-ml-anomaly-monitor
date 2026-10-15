import requests

try:
    resp = requests.get('http://localhost:9090/api/v1/label/__name__/values').json()
    metrics = resp.get('data', [])
    
    print("--- HPA Metrics ---")
    hpa_found = [m for m in metrics if 'hpa' in m.lower() or 'horizontalpodautoscaler' in m.lower()]
    for m in hpa_found: print(f"  - {m}")
    if not hpa_found: print("  (None found)")
            
    print("\n--- Application Metrics ---")
    app_found = [m for m in metrics if 'app' in m.lower() or 'work' in m.lower()]
    for m in app_found: print(f"  - {m}")
    if not app_found: print("  (None found)")
            
except Exception as e:
    print("Connection failed:", e)
