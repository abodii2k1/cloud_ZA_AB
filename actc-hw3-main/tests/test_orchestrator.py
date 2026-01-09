"""
Tests for basic orchestrator functionality.
"""

import pytest
import requests
import time

BASE_URL = "http://localhost:3000"


@pytest.fixture(scope="module")
def cleanup():
    """Clean up all resources before and after tests"""
    # Clean up before
    try:
        pods = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods").json()
        for pod in pods.get("items", []):
            name = pod["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/{name}")

        services = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services").json()
        for svc in services.get("items", []):
            name = svc["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/v1/namespaces/default/services/{name}")

        replicasets = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets").json()
        for rs in replicasets.get("items", []):
            name = rs["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/{name}")
    except:
        pass

    time.sleep(2)

    yield

    # Clean up after
    try:
        pods = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods").json()
        for pod in pods.get("items", []):
            name = pod["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/{name}")

        services = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services").json()
        for svc in services.get("items", []):
            name = svc["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/v1/namespaces/default/services/{name}")

        replicasets = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets").json()
        for rs in replicasets.get("items", []):
            name = rs["metadata"]["name"]
            requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/{name}")
    except:
        pass


def test_healthz():
    """Test health check endpoint"""
    resp = requests.get(f"{BASE_URL}/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_create_pod(cleanup):
    """Test creating a pod"""
    pod_spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "test-pod"},
        "spec": {
            "containers": [
                {
                    "name": "test",
                    "image": "health",
                    "ports": [{"containerPort": 8080}]
                }
            ]
        }
    }

    resp = requests.post(
        f"{BASE_URL}/api/v1/namespaces/default/pods",
        json=pod_spec
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["metadata"]["name"] == "test-pod"
    assert data["kind"] == "Pod"

    # Wait for reconciliation
    time.sleep(3)

    # Verify pod exists
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods/test-pod")
    assert resp.status_code == 200

    # Clean up
    requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/test-pod")


def test_list_pods(cleanup):
    """Test listing pods"""
    # Create two pods
    for i in range(2):
        pod_spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": f"test-pod-{i}"},
            "spec": {
                "containers": [
                    {"name": "test", "image": "health"}
                ]
            }
        }
        requests.post(f"{BASE_URL}/api/v1/namespaces/default/pods", json=pod_spec)

    time.sleep(2)

    # List pods
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "PodList"
    assert len(data["items"]) >= 2


def test_delete_pod(cleanup):
    """Test deleting a pod"""
    # Create pod
    pod_spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "delete-test"},
        "spec": {
            "containers": [{"name": "test", "image": "health"}]
        }
    }
    requests.post(f"{BASE_URL}/api/v1/namespaces/default/pods", json=pod_spec)
    time.sleep(2)

    # Delete pod
    resp = requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/delete-test")
    assert resp.status_code == 200

    time.sleep(2)

    # Verify pod is gone
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods/delete-test")
    assert resp.status_code == 404
