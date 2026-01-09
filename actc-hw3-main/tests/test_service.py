"""
Tests for Service functionality.
"""

import pytest
import requests
import time

BASE_URL = "http://localhost:3000"


@pytest.fixture(scope="function")
def cleanup_all():
    """Clean up all resources before and after each test"""
    def clean():
        try:
            # Delete services
            resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services")
            if resp.status_code == 200:
                for svc in resp.json().get("items", []):
                    name = svc["metadata"]["name"]
                    requests.delete(f"{BASE_URL}/api/v1/namespaces/default/services/{name}")

            # Delete replicasets
            resp = requests.get(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets")
            if resp.status_code == 200:
                for rs in resp.json().get("items", []):
                    name = rs["metadata"]["name"]
                    requests.delete(f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets/{name}")

            # Delete pods
            resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/pods")
            if resp.status_code == 200:
                for pod in resp.json().get("items", []):
                    name = pod["metadata"]["name"]
                    requests.delete(f"{BASE_URL}/api/v1/namespaces/default/pods/{name}")

            time.sleep(3)
        except:
            pass

    clean()
    yield
    clean()


def test_create_service(cleanup_all):
    """Test creating a Service"""
    svc_spec = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "test-service"},
        "spec": {
            "selector": {"app": "test"},
            "ports": [
                {"port": 8080, "targetPort": 8080}
            ]
        }
    }

    resp = requests.post(
        f"{BASE_URL}/api/v1/namespaces/default/services",
        json=svc_spec
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["metadata"]["name"] == "test-service"
    assert data["kind"] == "Service"


def test_service_with_pods(cleanup_all):
    """Test that Service creates LB when backend pods exist"""
    # Create backend pods via ReplicaSet
    rs_spec = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "backend-rs"},
        "spec": {
            "replicas": 2,
            "selector": {"app": "backend"},
            "template": {
                "metadata": {"app": "backend"},
                "spec": {
                    "containers": [
                        {
                            "name": "backend",
                            "image": "health",
                            "ports": [{"containerPort": 8080}]
                        }
                    ]
                }
            }
        }
    }

    requests.post(
        f"{BASE_URL}/api/apps/v1/namespaces/default/replicasets",
        json=rs_spec
    )

    # Wait for pods to be created
    time.sleep(6)

    # Create Service
    svc_spec = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "backend-service"},
        "spec": {
            "selector": {"app": "backend"},
            "ports": [
                {"port": 9000, "targetPort": 8080}
            ]
        }
    }

    resp = requests.post(
        f"{BASE_URL}/api/v1/namespaces/default/services",
        json=svc_spec
    )
    assert resp.status_code == 201

    # Wait for LB container to be created
    time.sleep(3)

    # Verify service exists
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services/backend-service")
    assert resp.status_code == 200


def test_list_services(cleanup_all):
    """Test listing services"""
    # Create two services
    for i in range(2):
        svc_spec = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"test-svc-{i}"},
            "spec": {
                "selector": {"app": f"test-{i}"},
                "ports": [{"port": 8080 + i}]
            }
        }
        requests.post(f"{BASE_URL}/api/v1/namespaces/default/services", json=svc_spec)

    time.sleep(2)

    # List services
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services")
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "ServiceList"
    assert len(data["items"]) >= 2


def test_delete_service(cleanup_all):
    """Test deleting a Service"""
    # Create service
    svc_spec = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "delete-svc"},
        "spec": {
            "selector": {"app": "delete"},
            "ports": [{"port": 8080}]
        }
    }
    requests.post(f"{BASE_URL}/api/v1/namespaces/default/services", json=svc_spec)
    time.sleep(2)

    # Delete service
    resp = requests.delete(f"{BASE_URL}/api/v1/namespaces/default/services/delete-svc")
    assert resp.status_code == 200

    # Verify service is gone
    resp = requests.get(f"{BASE_URL}/api/v1/namespaces/default/services/delete-svc")
    assert resp.status_code == 404
