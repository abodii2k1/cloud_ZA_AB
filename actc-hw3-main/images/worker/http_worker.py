#!/usr/bin/env python3
"""
Generic HTTP worker server for containerized deployment.
"""

import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Global state for aggregator
worker_state = {
    "messages": [],
    "count": 0
}


@app.route('/', methods=['GET'])
def root():
    """Root endpoint"""
    worker_type = os.getenv("WORKER_TYPE", "generic")
    return jsonify({"worker": worker_type, "status": "running"})


@app.route('/health', methods=['GET', 'POST'])
def health():
    """Health check - always returns healthy"""
    return jsonify({"healthy": True, "status": "ok"})


@app.route('/ping', methods=['GET', 'POST'])
def ping():
    """Ping health service and return result"""
    health_service = os.getenv("HEALTH_SERVICE")
    if health_service:
        try:
            resp = requests.get(f"http://{health_service}/health", timeout=5)
            return jsonify({"ping": "success", "health_response": resp.json()})
        except Exception as e:
            return jsonify({"ping": "failed", "error": str(e)}), 500
    return jsonify({"ping": "no target configured"})


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    worker_type = os.getenv("WORKER_TYPE", "generic")
    print(f"Starting {worker_type} worker on port {port}")
    app.run(host='0.0.0.0', port=port)
