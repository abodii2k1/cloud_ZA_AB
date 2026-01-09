#!/usr/bin/env python3
"""
Simple HTTP load balancer with round-robin distribution.
Reads backends from BACKENDS environment variable.
"""

import os
import sys
from flask import Flask, request, Response
import requests
import itertools

app = Flask(__name__)

# Parse backends from environment
backends_str = os.getenv("BACKENDS", "")
if not backends_str:
    print("ERROR: BACKENDS environment variable not set")
    sys.exit(1)

backends = [b.strip() for b in backends_str.split(",") if b.strip()]
if not backends:
    print("ERROR: No backends configured")
    sys.exit(1)

# Round-robin iterator
backend_cycle = itertools.cycle(backends)

print(f"Load balancer starting with backends: {backends}")


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def proxy(path):
    """Proxy all requests to backend servers in round-robin fashion"""
    backend = next(backend_cycle)
    url = f"http://{backend}/{path}"

    # Forward the request
    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers={k: v for k, v in request.headers if k.lower() != 'host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30
        )

        # Return backend response
        return Response(
            resp.content,
            status=resp.status_code,
            headers=dict(resp.headers)
        )
    except Exception as e:
        print(f"Error forwarding to {url}: {e}")
        return {"error": f"Backend unreachable: {backend}"}, 502


if __name__ == '__main__':
    port = int(os.getenv('SERVICE_PORT', '8080'))
    print(f"Starting load balancer on port {port}")
    app.run(host='0.0.0.0', port=port)
