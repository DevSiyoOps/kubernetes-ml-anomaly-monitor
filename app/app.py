import time
import math
from flask import Flask, request
from prometheus_client import Counter, generate_latest

app = Flask(__name__)
REQUESTS = Counter('app_requests_total', 'Total app requests')

@app.route('/work')
def work():
    REQUESTS.inc()
    ms = int(request.args.get('ms', 0))
    if ms > 0:
        # Simulate CPU load for 'ms' milliseconds
        end_time = time.time() + (ms / 1000.0)
        while time.time() < end_time:
            math.factorial(100)
    return "Work completed\n"

@app.route('/metrics')
def metrics():
    return generate_latest()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)