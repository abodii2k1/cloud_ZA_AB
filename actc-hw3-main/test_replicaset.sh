#!/bin/bash

# Test ReplicaSet functionality
# This script tests creating, scaling, and deleting ReplicaSets

BASE_URL="http://localhost:3000"

echo "=== Testing ReplicaSet Functionality ==="
echo

# Test 1: Create a ReplicaSet with 3 replicas
echo "Test 1: Creating ReplicaSet with 3 replicas..."
curl -s -X POST $BASE_URL/api/apps/v1/namespaces/default/replicasets \
  -H "Content-Type: application/json" \
  -d '{
    "apiVersion": "apps/v1",
    "kind": "ReplicaSet",
    "metadata": {"name": "test-rs"},
    "spec": {
      "replicas": 3,
      "selector": {"app": "test"},
      "template": {
        "metadata": {"app": "test"},
        "spec": {
          "containers": [{"name": "test", "image": "alpine:latest"}]
        }
      }
    }
  }' | jq .

echo
echo "Waiting 5 seconds for reconciliation..."
sleep 5

# Test 2: List pods and count how many match
echo
echo "Test 2: Checking if 3 pods were created..."
POD_COUNT=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq '.items | length')
echo "Found $POD_COUNT pods"

# Also check with podman
echo "Podman container count:"
podman ps --filter label=app=test --format "{{.Names}}" | wc -l

# Test 3: Get ReplicaSet status
echo
echo "Test 3: Getting ReplicaSet status..."
curl -s $BASE_URL/api/apps/v1/namespaces/default/replicasets/test-rs | jq '.status'

# Test 4: Scale up to 5 replicas
echo
echo "Test 4: Scaling up to 5 replicas..."
curl -s -X PUT $BASE_URL/api/apps/v1/namespaces/default/replicasets/test-rs \
  -H "Content-Type: application/json" \
  -d '{
    "apiVersion": "apps/v1",
    "kind": "ReplicaSet",
    "metadata": {"name": "test-rs"},
    "spec": {
      "replicas": 5,
      "selector": {"app": "test"},
      "template": {
        "metadata": {"app": "test"},
        "spec": {
          "containers": [{"name": "test", "image": "alpine:latest"}]
        }
      }
    }
  }' | jq .

echo
echo "Waiting 5 seconds for reconciliation..."
sleep 5

echo
echo "Checking pod count after scaling up..."
POD_COUNT=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq '.items | length')
echo "Found $POD_COUNT pods (expected 5)"

# Test 5: Scale down to 2 replicas
echo
echo "Test 5: Scaling down to 2 replicas..."
curl -s -X PUT $BASE_URL/api/apps/v1/namespaces/default/replicasets/test-rs \
  -H "Content-Type: application/json" \
  -d '{
    "apiVersion": "apps/v1",
    "kind": "ReplicaSet",
    "metadata": {"name": "test-rs"},
    "spec": {
      "replicas": 2,
      "selector": {"app": "test"},
      "template": {
        "metadata": {"app": "test"},
        "spec": {
          "containers": [{"name": "test", "image": "alpine:latest"}]
        }
      }
    }
  }' | jq .

echo
echo "Waiting 5 seconds for reconciliation..."
sleep 5

echo
echo "Checking pod count after scaling down..."
POD_COUNT=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq '.items | length')
echo "Found $POD_COUNT pods (expected 2)"

# Test 6: Delete one pod manually and watch it get recreated
echo
echo "Test 6: Testing pod auto-recovery..."
FIRST_POD=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq -r '.items[0].metadata.name')
echo "Manually stopping pod: $FIRST_POD"
podman stop "default-$FIRST_POD"

echo "Waiting 6 seconds for reconciliation..."
sleep 6

echo "Checking pod count (should still be 2)..."
POD_COUNT=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq '.items | length')
echo "Found $POD_COUNT pods (expected 2)"

# Test 7: Delete ReplicaSet
echo
echo "Test 7: Deleting ReplicaSet..."
curl -s -X DELETE $BASE_URL/api/apps/v1/namespaces/default/replicasets/test-rs | jq .

echo
echo "Waiting 3 seconds..."
sleep 3

echo "Checking pod count (should be 0 after cascade delete)..."
POD_COUNT=$(curl -s $BASE_URL/api/v1/namespaces/default/pods | jq '.items | length')
echo "Found $POD_COUNT pods (expected 0)"

echo
echo "=== Test Complete ==="
