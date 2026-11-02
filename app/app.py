"""hpa-app v3: same /work behavior and Prometheus metrics as v2, with a full connection record per request.

Each request writes one JSON line to stdout (read it with `kubectl logs`):

  who      client_ip, client_port   the address the TCP connection came from
           server_ip, server_port   the address it arrived at (the pod IP and 8080 inside the cluster)
  how      transport (TCP), proto (HTTP/1.0 or HTTP/1.1), scheme, method, path, query, connection header
  what     status, req_body_bytes, resp_bytes, duration_ms, host, user_agent, xff (X-Forwarded-For)
  where    pod, request_id (also returned to the caller as the X-Request-ID header)

Notes on the source address
  * Traffic sent by a pod through the Service keeps the sender's pod IP.
  * `kubectl port-forward` always shows 127.0.0.1, because the connection is opened inside the pod.
  * NodePort or LoadBalancer traffic may show the node IP (source NAT). xff shows the original address
    only when a proxy in front sets it, and a client can forge it, so use client_ip as the truth.
  * The built-in server closes the connection after every response, so each request is its own TCP
    connection and client_port changes from one request to the next.
"""
import json
import logging
import math
import os
import socket
import sys
import time
import uuid

from flask import Flask, Response, g, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # keep stdout for our JSON lines only

POD = os.getenv("HOSTNAME", socket.gethostname())
MAX_WORK_MS = 5000
MAX_FIELD = 200  # longest value kept for free-text fields such as the user agent

REQUESTS = Counter("app_requests_total", "Total app requests")
LATENCY = Histogram(
    "app_request_duration_seconds",
    "Request latency in seconds",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
INFLIGHT = Gauge("app_inflight_requests", "Requests currently being handled")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(value):
    return value[:MAX_FIELD] if isinstance(value, str) else value


def local_endpoint(environ):
    """(ip, port) the connection arrived at. The built-in server exposes the connection socket;
    with another WSGI server (gunicorn) fall back to POD_IP / the configured address."""
    sock = environ.get("werkzeug.socket")
    if sock is not None:
        try:
            addr = sock.getsockname()
            return addr[0], addr[1]
        except OSError:
            pass
    return os.getenv("POD_IP") or environ.get("SERVER_NAME"), _int(environ.get("SERVER_PORT"))


def access_record(response, elapsed):
    env = request.environ
    server_ip, server_port = local_endpoint(env)
    return {
        "ts": round(time.time(), 3),
        "request_id": g.request_id,
        "pod": POD,
        "client_ip": request.remote_addr,
        "client_port": _int(env.get("REMOTE_PORT")),
        "server_ip": server_ip,
        "server_port": server_port,
        "transport": "TCP",
        "proto": env.get("SERVER_PROTOCOL"),
        "scheme": request.scheme,
        "method": request.method,
        "path": _clip(request.path),
        "query": _clip(request.query_string.decode("latin-1")),
        "status": response.status_code,
        "host": _clip(request.host),
        "user_agent": _clip(request.headers.get("User-Agent")),
        "xff": _clip(request.headers.get("X-Forwarded-For")),
        "connection": request.headers.get("Connection"),
        "req_body_bytes": request.content_length or 0,
        "resp_bytes": response.calculate_content_length() or 0,
        "duration_ms": round(elapsed * 1000, 2),
    }


@app.before_request
def _start():
    g.tracked = request.path != "/metrics"
    if g.tracked:
        g.t0 = time.perf_counter()
        g.request_id = uuid.uuid4().hex[:12]
        INFLIGHT.inc()


@app.after_request
def _finish(response):
    if getattr(g, "tracked", False):
        elapsed = time.perf_counter() - g.t0
        LATENCY.observe(elapsed)
        INFLIGHT.dec()
        g.tracked = False
        response.headers["X-Request-ID"] = g.request_id
        sys.stdout.write(json.dumps(access_record(response, elapsed)) + "\n")
        sys.stdout.flush()
    return response


@app.teardown_request
def _cleanup(exc):
    # If the handler crashed before after_request ran, still release the in-flight slot.
    if getattr(g, "tracked", False):
        INFLIGHT.dec()
        g.tracked = False


@app.route("/work")
def work():
    REQUESTS.inc()
    try:
        ms = int(request.args.get("ms", 0))
    except ValueError:
        return "ms must be an integer\n", 400
    ms = max(0, min(ms, MAX_WORK_MS))
    if ms > 0:
        # Simulate CPU load for 'ms' milliseconds
        end_time = time.time() + (ms / 1000.0)
        while time.time() < end_time:
            math.factorial(100)
    return "Work completed\n"


@app.route("/metrics")
def metrics():
    # content_type (not mimetype): Flask would append a second "charset" to the string, and
    # Prometheus can reject a content type with a duplicated parameter.
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))