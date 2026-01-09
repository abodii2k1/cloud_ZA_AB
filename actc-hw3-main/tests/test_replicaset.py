"""
Tests for ReplicaSet functionality.
"""

import pytest
import requests
import time

BASE_URL = "http://localhost:3000"


@pytest.fixture(scope="function")
def cleanup_replicasets():
    """Clean up all replicasets and pods before each test"""
    try:
        # Delete all replicasets
        resp = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets")
        if resp.status_code == 200:
            for rs in resp.json().get("items", []):
                name = rs["metadata"]["name"]
                requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/{name}")

        # Delete all pods
        resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
        if resp.status_code == 200:
            for pod in resp.json().get("items", []):
                name = pod["metadata"]["name"]
                requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/{name}")

        time.sleep(3)
    except:
        pass

    yield

    # Clean up after test
    try:
        resp = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets")
        if resp.status_code == 200:
            for rs in resp.json().get("items", []):
                name = rs["metadata"]["name"]
                requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/{name}")

        time.sleep(2)
    except:
        pass


def test_create_replicaset(cleanup_replicasets):
    """Test creating a ReplicaSet with 3 replicas"""
    rs_spec = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "test-rs"},
        "spec": {
            "replicas": 3,
            "selector": {"app": "test"},
            "template": {
                "metadata": {"app": "test"},
                "spec": {
                    "containers": [
                        {"name": "test", "image": "health"}
                    ]
                }
            }
        }
    }

    resp = requests.post(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets",
        json=rs_spec
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["metadata"]["name"] == "test-rs"
    assert data["spec"]["replicas"] == 3

    # Wait for reconciliation to create pods
    time.sleep(6)

    # Check that 3 pods were created
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    assert resp.status_code == 200
    pods = resp.json()["items"]
    test_pods = [p for p in pods if p["metadata"]["name"].startswith("test-rs-")]
    assert len(test_pods) == 3

    # Check ReplicaSet status
    resp = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/test-rs")
    assert resp.status_code == 200
    rs = resp.json()
    assert rs["status"]["replicas"] == 3


def test_scale_up_replicaset(cleanup_replicasets):
    """Test scaling up a ReplicaSet from 2 to 5 replicas"""
    # Create ReplicaSet with 2 replicas
    rs_spec = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "scale-test"},
        "spec": {
            "replicas": 2,
            "selector": {"app": "scale"},
            "template": {
                "metadata": {"app": "scale"},
                "spec": {
                    "containers": [{"name": "test", "image": "health"}]
                }
            }
        }
    }

    requests.post(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets",
        json=rs_spec
    )
    time.sleep(6)

    # Verify 2 pods
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    pods = resp.json()["items"]
    scale_pods = [p for p in pods if p["metadata"]["name"].startswith("scale-test-")]
    assert len(scale_pods) == 2

    # Scale up to 5
    rs_spec["spec"]["replicas"] = 5
    resp = requests.put(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/scale-test",
        json=rs_spec
    )
    assert resp.status_code == 200

    # Wait for new pods
    time.sleep(6)

    # Verify 5 pods
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    pods = resp.json()["items"]
    scale_pods = [p for p in pods if p["metadata"]["name"].startswith("scale-test-")]
    assert len(scale_pods) == 5


def test_scale_down_replicaset(cleanup_replicasets):
    """Test scaling down a ReplicaSet from 4 to 1 replica"""
    # Create ReplicaSet with 4 replicas
    rs_spec = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "scale-down-test"},
        "spec": {
            "replicas": 4,
            "selector": {"app": "scaledown"},
            "template": {
                "metadata": {"app": "scaledown"},
                "spec": {
                    "containers": [{"name": "test", "image": "health"}]
                }
            }
        }
    }

    requests.post(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets",
        json=rs_spec
    )
    time.sleep(6)

    # Scale down to 1
    rs_spec["spec"]["replicas"] = 1
    requests.put(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/scale-down-test",
        json=rs_spec
    )
    time.sleep(6)

    # Verify 1 pod
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    pods = resp.json()["items"]
    scale_pods = [p for p in pods if p["metadata"]["name"].startswith("scale-down-test-")]
    assert len(scale_pods) == 1


def test_delete_replicaset_cascade(cleanup_replicasets):
    """Test that deleting a ReplicaSet also deletes its pods"""
    # Create ReplicaSet
    rs_spec = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "delete-test"},
        "spec": {
            "replicas": 3,
            "selector": {"app": "delete"},
            "template": {
                "metadata": {"app": "delete"},
                "spec": {
                    "containers": [{"name": "test", "image": "health"}]
                }
            }
        }
    }

    requests.post(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets",
        json=rs_spec
    )
    time.sleep(6)

    # Verify pods exist
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    pods = resp.json()["items"]
    delete_pods = [p for p in pods if p["metadata"]["name"].startswith("delete-test-")]
    assert len(delete_pods) == 3

    # Delete ReplicaSet
    resp = requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/delete-test")
    assert resp.status_code == 200

    time.sleep(3)

    # Verify pods are deleted
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
    pods = resp.json()["items"]
    delete_pods = [p for p in pods if p["metadata"]["name"].startswith("delete-test-")]
    assert len(delete_pods) == 0
