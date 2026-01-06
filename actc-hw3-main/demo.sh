#!/bin/bash
#
# Demo script for Kubernetes-like Orchestration System
# Runs orchestrator.py in the background and demonstrates features with curl commands
#

set -e

ORCHESTRATOR="http://localhost:3000"
ORCHESTRATOR_PID=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

#------------------------------------------------------------------------------
# Helper functions
#------------------------------------------------------------------------------

print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
}

print_step() {
    echo -e "${GREEN}>>> $1${NC}"
}

print_info() {
    echo -e "${YELLOW}$1${NC}"
}

wait_for_orchestrator() {
    print_step "Waiting for orchestrator to be ready..."
    for i in {1..30}; do
        if curl -s "$ORCHESTRATOR/healthz" > /dev/null 2>&1; then
            print_info "Orchestrator is ready!"
            return 0
        fi
        sleep 0.5
    done
    echo "ERROR: Orchestrator failed to start"
    exit 1
}

wait_for_pods() {
    local count=$1
    local label_key=$2
    local label_value=$3
    print_step "Waiting for $count pods to be ready..."
    sleep 3
}

wait_for_user() {
    echo ""
    echo -e "${YELLOW}Press [Enter] to continue to next demo...${NC}"
    read -r
}

cleanup_all() {
    print_step "Cleaning up all resources..."
    
    # Delete all pods
    pods=$(curl -s "$ORCHESTRATOR/api/v1/namespaces/default/pods" | uv run python3 -c "import sys, json; items = json.load(sys.stdin).get('items', []); print(' '.join([p['metadata']['name'] for p in items]))" 2>/dev/null || echo "")
    for pod in $pods; do
        curl -s -X DELETE "$ORCHESTRATOR/api/v1/namespaces/default/pods/$pod" > /dev/null 2>&1 || true
    done
    
    # Delete all services
    services=$(curl -s "$ORCHESTRATOR/api/v1/namespaces/default/services" | uv run python3 -c "import sys, json; items = json.load(sys.stdin).get('items', []); print(' '.join([s['metadata']['name'] for s in items]))" 2>/dev/null || echo "")
    for svc in $services; do
        curl -s -X DELETE "$ORCHESTRATOR/api/v1/namespaces/default/services/$svc" > /dev/null 2>&1 || true
    done
    
    sleep 2
    print_info "Cleanup complete"
}

start_orchestrator() {
    print_header "Starting Orchestrator"
    
    # Kill any existing orchestrator
    pkill -f "python.*orchestrator.py" 2>/dev/null || true
    sleep 1
    
    # Start orchestrator in background
    uv run python3 orchestrator.py --port 3000 &
    ORCHESTRATOR_PID=$!
    
    wait_for_orchestrator
}

stop_orchestrator() {
    print_step "Stopping orchestrator..."
    if [ -n "$ORCHESTRATOR_PID" ]; then
        kill $ORCHESTRATOR_PID 2>/dev/null || true
    fi
    pkill -f "python.*orchestrator.py" 2>/dev/null || true
}

# Trap to ensure cleanup on exit
trap stop_orchestrator EXIT

#------------------------------------------------------------------------------
# Demo 1: Health and Ping (Basic Pod Communication)
#------------------------------------------------------------------------------

demo_health_ping() {
    print_header "DEMO 1: Health and Ping - Basic Pod Communication"
    
    print_info "This demo shows:"
    print_info "  - Creating a Health pod"
    print_info "  - Creating a Health service"
    print_info "  - Creating a Ping pod that calls the health service"
    echo ""
    
    print_step "Creating health pod"
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "health",
                "app": "health"
            },
            "spec": {
                "containers": [{
                    "name": "health",
                    "image": "health"
                }]
            }
        }' | uv run python3 -m json.tool
    
    sleep 3
    
    print_step "Creating health Service..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/services" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "health-service"
            },
            "spec": {
                "selector": {
                    "app": "health"
                },
                "ports": [{
                    "protocol": "TCP",
                    "port": 2000,
                    "targetPort": 5000
                }],
                "type": "ClusterIP"
            }
        }' | uv run python3 -m json.tool
    
    sleep 1
    
    print_step "Creating ping Pod that calls health service..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "ping"
            },
            "spec": {
                "containers": [{
                    "name": "ping",
                    "image": "ping",
                    "env": {
                        "HEALTH_SERVICE": "health-service:2000"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    print_step "Listing all pods..."
    curl -s "$ORCHESTRATOR/api/v1/namespaces/default/pods" | uv run python3 -m json.tool
    
    print_info "Watch the ping pod call the health service..."
    sleep 5
    
    cleanup_all
}

#------------------------------------------------------------------------------
# Demo 2: Inter-Pod Communication
#------------------------------------------------------------------------------

demo_inter_pod() {
    print_header "DEMO 2: Inter-Pod Communication"
    
    print_info "This demo shows:"
    print_info "  - Processor pod forwarding to aggregator pod"
    print_info "  - Aggregator collecting messages in windows"
    echo ""
    
    print_step "Creating aggregator Pod..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "aggregator"
            },
            "spec": {
                "containers": [{
                    "name": "aggregator",
                    "image": "aggregator_worker",
                    "env": {
                        "window_size": "3"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    sleep 2
    
    print_step "Creating processor Pod that forwards to aggregator..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "processor"
            },
            "spec": {
                "containers": [{
                    "name": "processor",
                    "image": "processor_worker",
                    "env": {
                        "operation": "uppercase",
                        "forward_to": "aggregator"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    sleep 2
    
    print_step "Sending messages to processor (which forwards to aggregator)..."
    for i in {1..5}; do
        echo "Sending test-$i..."
        curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods/processor/send" \
            -H "Content-Type: application/json" \
            -d "{\"data\": \"test-$i\"}"
        sleep 0.5
    done
    
    sleep 2
    cleanup_all
}

#------------------------------------------------------------------------------
# Demo 3: Generator -> Service Pipeline
#------------------------------------------------------------------------------

demo_generator_pipeline() {
    print_header "DEMO 3: Generator -> Service Pipeline"
    
    print_info "This demo shows:"
    print_info "  - Generator pod sending to a processor pod"
    print_info "  - Processor pod handling messages"
    echo ""
    
    print_step "Creating processor Pod..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "processor"
            },
            "spec": {
                "containers": [{
                    "name": "processor",
                    "image": "processor_worker",
                    "env": {
                        "operation": "reverse"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    print_step "Creating generator Pod..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "generator"
            },
            "spec": {
                "containers": [{
                    "name": "generator",
                    "image": "generator_worker",
                    "env": {
                        "target": "processor",
                        "interval": "1",
                        "count": "5"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    print_info "Generator will send messages to processor..."
    sleep 8
    
    cleanup_all
}

#------------------------------------------------------------------------------
# Demo 4: CRUD Operations
#------------------------------------------------------------------------------

demo_crud() {
    print_header "DEMO 4: Resource CRUD Operations"
    
    print_info "This demo shows:"
    print_info "  - Create, Read, Update, Delete operations on resources"
    echo ""
    
    print_step "1. CREATE - Creating a health pod..."
    curl -s -X POST "$ORCHESTRATOR/api/v1/namespaces/default/pods" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "test-pod",
                "labels": {
                    "app": "test"
                }
            },
            "spec": {
                "containers": [{
                    "name": "health",
                    "image": "health"
                }]
            }
        }' | uv run python3 -m json.tool
    
    sleep 2
    
    print_step "2. READ - Getting the pod..."
    curl -s "$ORCHESTRATOR/api/v1/namespaces/default/pods/test-pod" | uv run python3 -m json.tool
    
    print_step "3. UPDATE - Updating the pod (adding env var)..."
    curl -s -X PUT "$ORCHESTRATOR/api/v1/namespaces/default/pods/test-pod" \
        -H "Content-Type: application/json" \
        -d '{
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "test-pod",
                "labels": {
                    "app": "test",
                    "version": "v2"
                }
            },
            "spec": {
                "containers": [{
                    "name": "health",
                    "image": "health",
                    "env": {
                        "MY_ENV_VAR": "value"
                    }
                }]
            }
        }' | uv run python3 -m json.tool
    
    sleep 2
    
    print_step "4. DELETE - Deleting the pod..."
    curl -s -X DELETE "$ORCHESTRATOR/api/v1/namespaces/default/pods/test-pod" | uv run python3 -m json.tool
    
    print_step "5. VERIFY - Trying to get deleted pod (should 404)..."
    curl -s "$ORCHESTRATOR/api/v1/namespaces/default/pods/test-pod" | uv run python3 -m json.tool
    
    cleanup_all
}

#------------------------------------------------------------------------------
# Main
#------------------------------------------------------------------------------

main() {
    echo ""
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║     Kubernetes-like Orchestration System Demo              ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "This demo will show:"
    echo "  1. Health and Ping - Basic Pod Communication"
    echo "  2. Inter-Pod Communication"
    echo "  3. Generator -> Service Pipeline"
    echo "  4. Resource CRUD Operations"
    echo ""
    
    start_orchestrator
    
    # Run demos with user interaction
    demo_health_ping
    wait_for_user
    
    demo_inter_pod
    wait_for_user
    
    demo_generator_pipeline
    wait_for_user
    
    demo_crud
    
    print_header "All Demos Completed!"
    echo -e "${GREEN}Thank you for watching the Orchestrator demo!${NC}"
    echo ""
}

# Run main
main
