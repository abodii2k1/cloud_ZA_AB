"""
Pytest configuration and shared fixtures.
"""

import pytest
import subprocess
import time
import requests


def is_orchestrator_running():
    """Check if orchestrator is running"""
    try:
        resp = requests.get("http://localhost:3000/healthz", timeout=2)
        return resp.status_code == 200
    except:
        return False


@pytest.fixture(scope="session", autouse=True)
def ensure_orchestrator_running():
    """Ensure orchestrator is running before tests"""
    if not is_orchestrator_running():
        pytest.fail(
            "Orchestrator is not running on http://localhost:3000\n"
            "Please start it with: python orchestrator.py --port 3000"
        )
    yield
