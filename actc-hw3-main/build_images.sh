#!/bin/bash
# Build all container images for the orchestrator

set -e

echo "Building container images..."

# Build load balancer image
echo "Building orchestrator-lb..."
podman build -t orchestrator-lb ./images/lb/

# Build worker image
echo "Building orchestrator-worker..."
podman build -t orchestrator-worker ./images/worker/

# Tag worker image with specific names for compatibility
podman tag orchestrator-worker health
podman tag orchestrator-worker ping

echo "Container images built successfully!"
echo ""
echo "Available images:"
podman images | grep -E "(orchestrator|health|ping)"
