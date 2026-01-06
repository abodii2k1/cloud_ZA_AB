"""
Kubernetes-like orchestration system for Python threads.

This implements:
- Resource types: Pod, Service (strict subset of Kubernetes API)
- Controllers for each resource type
- In-memory resource tree
- Inter-container communication via queues
- FastAPI REST API compatible with kubectl-style requests
"""

import threading
import queue
import yaml
import random
import importlib
import time
import re
import uvicorn
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from concurrent.futures import Future
from enum import Enum
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel


# =============================================================================
# Resource Definitions
# =============================================================================


class ResourceType(Enum):
    POD = "pod"
    SERVICE = "service"


@dataclass
class Resource:
    """Base resource class"""

    api_version: str
    kind: str
    name: str
    namespace: str
    spec: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert resource to Kubernetes-style dict"""
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "metadata": {
                "name": self.name,
                "namespace": self.namespace,
                **self.metadata,
            },
            "spec": self.spec,
            "status": self.status,
        }


@dataclass
class PodResource(Resource):
    """Pod resource definition"""

    def __init__(
        self,
        name: str,
        spec: Dict[str, Any],
        metadata: Dict[str, Any] = None,
        namespace: str = "default",
    ):
        super().__init__(
            api_version="v1",
            kind="Pod",
            name=name,
            namespace=namespace,
            spec=spec,
            metadata=metadata or {},
        )

    @property
    def containers(self) -> List[Dict[str, Any]]:
        return self.spec.get("containers", [])

    @property
    def first_container(self) -> Optional[Dict[str, Any]]:
        containers = self.containers
        return containers[0] if containers else None

    @property
    def image(self) -> Optional[str]:
        container = self.first_container
        return container.get("image") if container else None

    @property
    def env(self) -> Dict[str, Any]:
        container = self.first_container
        if container:
            env = container.get("env", {})
            # Handle both dict format and list format
            if isinstance(env, list):
                return {
                    item["name"]: item["value"]
                    for item in env
                    if "name" in item and "value" in item
                }
            return env
        return {}

    @property
    def labels(self) -> Dict[str, str]:
        return self.metadata


@dataclass
class ServiceResource(Resource):
    """Service resource definition"""

    def __init__(
        self,
        name: str,
        spec: Dict[str, Any],
        metadata: Dict[str, Any] = None,
        namespace: str = "default",
    ):
        super().__init__(
            api_version="v1",
            kind="Service",
            name=name,
            namespace=namespace,
            spec=spec,
            metadata=metadata or {},
        )

    @property
    def selector(self) -> Dict[str, str]:
        return self.spec.get("selector", {})

    @property
    def ports(self) -> List[Dict[str, Any]]:
        return self.spec.get("ports", [])

    @property
    def service_type(self) -> str:
        return self.spec.get("type", "ClusterIP")


# =============================================================================
# Container Runtime
# =============================================================================


class Container:
    """Running container (thread) instance"""

    def __init__(
        self,
        name: str,
        image: str,
        env: Dict[str, Any],
        api_client: "OrchestratorAPI",
        labels: Dict[str, str] = None,
    ):
        self.name = name
        self.image = image
        self.env = env
        self.api_client = api_client
        self.labels = labels or {}
        self.input_queue = queue.Queue()
        self.thread = None
        self.running = False

    def start(self):
        """Start the container thread"""
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the container thread"""
        self.running = False
        self.input_queue.put(None)  # Signal to stop
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        """Run the container function"""
        try:
            # Image format: module.function or just function (assumes workers module)
            if "." in self.image:
                module_name, function_name = self.image.rsplit(".", 1)
            else:
                module_name = "workers"
                function_name = self.image

            # Dynamically import the module and get the function
            module = importlib.import_module(module_name)
            func = getattr(module, function_name)

            # Run the function with env and input queue
            func(
                input_queue=self.input_queue,
                api_client=self.api_client,
                **self.env,
            )
        except Exception as e:
            print(f"Container {self.name} error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self.running = False


# =============================================================================
# Resource Store
# =============================================================================


class ResourceStore:
    """In-memory resource tree"""

    def __init__(self):
        self.resources: Dict[str, Dict[str, Dict[str, Resource]]] = {
            "Pod": {},  # namespace -> name -> resource
            "Service": {},
        }
        self.lock = threading.RLock()

    def _ensure_namespace(self, kind: str, namespace: str):
        """Ensure namespace dict exists"""
        if namespace not in self.resources[kind]:
            self.resources[kind][namespace] = {}

    def create(self, resource: Resource) -> Resource:
        """Create a resource"""
        with self.lock:
            self._ensure_namespace(resource.kind, resource.namespace)
            if resource.name in self.resources[resource.kind][resource.namespace]:
                raise ValueError(
                    f"{resource.kind} {resource.namespace}/{resource.name} already exists"
                )
            self.resources[resource.kind][resource.namespace][resource.name] = resource
            return resource

    def get(
        self, kind: str, name: str, namespace: str = "default"
    ) -> Optional[Resource]:
        """Get a resource by kind, namespace, and name"""
        with self.lock:
            return self.resources.get(kind, {}).get(namespace, {}).get(name)

    def list(self, kind: str, namespace: str = None) -> List[Resource]:
        """List all resources of a kind, optionally filtered by namespace"""
        with self.lock:
            if namespace:
                return list(self.resources.get(kind, {}).get(namespace, {}).values())
            else:
                # Return all resources across all namespaces
                result = []
                for ns_resources in self.resources.get(kind, {}).values():
                    result.extend(ns_resources.values())
                return result

    def update(self, resource: Resource) -> Resource:
        """Update a resource"""
        with self.lock:
            self._ensure_namespace(resource.kind, resource.namespace)
            if resource.name not in self.resources[resource.kind][resource.namespace]:
                raise ValueError(
                    f"{resource.kind} {resource.namespace}/{resource.name} does not exist"
                )
            self.resources[resource.kind][resource.namespace][resource.name] = resource
            return resource

    def delete(self, kind: str, name: str, namespace: str = "default") -> bool:
        """Delete a resource"""
        with self.lock:
            ns_resources = self.resources.get(kind, {}).get(namespace, {})
            if name in ns_resources:
                del ns_resources[name]
                return True
            return False

    def create_or_update(self, resource: Resource) -> Resource:
        """Create or update a resource"""
        with self.lock:
            self._ensure_namespace(resource.kind, resource.namespace)
            self.resources[resource.kind][resource.namespace][resource.name] = resource
            return resource


# =============================================================================
# Controllers
# =============================================================================


class Controller:
    """Base controller class"""

    def __init__(self, store: ResourceStore):
        self.store = store
        self.running = False
        self.thread = None

    def start(self):
        """Start the controller"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._reconcile_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the controller"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _reconcile_loop(self):
        """Main reconciliation loop"""
        while self.running:
            try:
                self.reconcile()
            except Exception as e:
                print(f"Controller {self.__class__.__name__} error: {e}")
                import traceback

                traceback.print_exc()
            time.sleep(1)

    def reconcile(self):
        """Reconcile desired state with actual state"""
        raise NotImplementedError


class PodController(Controller):
    """Controller for Pod resources"""

    def __init__(self, store: ResourceStore, api_client: "OrchestratorAPI"):
        super().__init__(store)
        self.api_client = api_client
        self.containers: Dict[str, Container] = {}  # "namespace/name" -> Container
        self.lock = threading.RLock()

    def _container_key(self, namespace: str, name: str) -> str:
        return f"{namespace}/{name}"

    def reconcile(self):
        """Ensure containers match their Pod definitions"""
        with self.lock:
            # Get desired pods
            desired_pods = {}
            for pod in self.store.list("Pod"):
                key = self._container_key(pod.namespace, pod.name)
                desired_pods[key] = pod

            # Stop and remove containers that shouldn't exist
            for key in list(self.containers.keys()):
                if key not in desired_pods:
                    print(f"Stopping pod: {key}")
                    self.containers[key].stop()
                    del self.containers[key]

            # Create containers for new pods
            for key, pod in desired_pods.items():
                if key not in self.containers:
                    print(f"Starting pod: {key}")
                    container = Container(
                        name=pod.name,
                        image=pod.image,
                        env=pod.env,
                        api_client=self.api_client,
                        labels=pod.labels,
                    )
                    container.start()
                    self.containers[key] = container
                    # Update pod status
                    pod.status = {"phase": "Running"}

    def get_container(
        self, name: str, namespace: str = "default"
    ) -> Optional[Container]:
        """Get a running container by name"""
        with self.lock:
            key = self._container_key(namespace, name)
            return self.containers.get(key)

    def list_containers(self, namespace: str = None) -> List[Container]:
        """List all running containers, optionally filtered by namespace"""
        with self.lock:
            if namespace:
                return [
                    c
                    for key, c in self.containers.items()
                    if key.startswith(f"{namespace}/")
                ]
            return list(self.containers.values())

    def get_containers_by_labels(
        self, selector: Dict[str, str], namespace: str = "default"
    ) -> List[Container]:
        """Get containers matching label selector"""
        with self.lock:
            result = []
            for key, container in self.containers.items():
                if not key.startswith(f"{namespace}/"):
                    continue
                # Check if all selector labels match
                matches = all(container.labels.get(k) == v for k, v in selector.items())
                if matches:
                    result.append(container)
            return result


class ServiceController(Controller):
    """Controller for Service resources - mainly for validation"""

    def __init__(self, store: ResourceStore, pod_controller: PodController):
        super().__init__(store)
        self.pod_controller = pod_controller

    def reconcile(self):
        """Validate service selectors"""
        for service in self.store.list("Service"):
            # Validate that the selector matches at least one pod
            containers = self.pod_controller.get_containers_by_labels(
                service.selector, service.namespace
            )
            if not containers:
                print(
                    f"Warning: Service {service.name} selector {service.selector} matches no pods"
                )


# =============================================================================
# API
# =============================================================================


class OrchestratorAPI:
    """API for interacting with containers"""

    def __init__(self, pod_controller: PodController, store: ResourceStore):
        self.pod_controller = pod_controller
        self.store = store

    def send_to_pod(
        self,
        pod_name: str,
        value: Any,
        namespace: str = "default",
        expect_response: bool = False,
    ) -> Optional[Future]:
        """Send a value to a pod's container queue"""
        container = self.pod_controller.get_container(pod_name, namespace)
        if not container:
            raise ValueError(f"Pod {namespace}/{pod_name} not found or not running")

        if expect_response:
            future = Future()
            container.input_queue.put((value, future))
            return future
        else:
            container.input_queue.put(value)
            return None

    def send_to_service(
        self,
        service_name: str,
        value: Any,
        namespace: str = "default",
        expect_response: bool = False,
        port: int = None,
    ) -> Optional[Future]:
        """Send a value to a random pod matched by the service"""
        service = self.store.get("Service", service_name, namespace)
        if not service:
            raise ValueError(f"Service {namespace}/{service_name} not found")

        # Find pods matching the service selector
        containers = self.pod_controller.get_containers_by_labels(
            service.selector, namespace
        )

        if not containers:
            raise ValueError(f"No pods match service {service_name}")

        # Random load balancing
        container = random.choice(containers)

        if expect_response:
            future = Future()
            container.input_queue.put((value, future))
            return future
        else:
            container.input_queue.put(value)
            return None

    def resolve_service(self, service_ref: str, namespace: str = "default") -> tuple:
        """Resolve a service reference like 'service-name:port' to (service, port)"""
        if ":" in service_ref:
            service_name, port = service_ref.rsplit(":", 1)
            port = int(port)
        else:
            service_name = service_ref
            port = None

        service = self.store.get("Service", service_name, namespace)
        return service, port


# =============================================================================
# Cluster
# =============================================================================


class OrchestratorCluster:
    """Main cluster orchestrator"""

    def __init__(self):
        self.store = ResourceStore()
        self.pod_controller = None
        self.service_controller = None
        self.api = None
        self._started = False

    def start(self):
        """Start the cluster"""
        if self._started:
            return

        # Initialize API
        self.pod_controller = PodController(self.store, None)
        self.api = OrchestratorAPI(self.pod_controller, self.store)
        self.pod_controller.api_client = self.api

        # Initialize controllers
        self.service_controller = ServiceController(self.store, self.pod_controller)

        # Start controllers
        self.pod_controller.start()
        self.service_controller.start()

        self._started = True
        print("Orchestrator cluster started")

    def stop(self):
        """Stop the cluster"""
        if self.pod_controller:
            self.pod_controller.stop()
        if self.service_controller:
            self.service_controller.stop()
        self._started = False
        print("Orchestrator cluster stopped")

    def apply_resource(
        self, resource_dict: Dict[str, Any], namespace: str = "default"
    ) -> Resource:
        """Apply a resource from a dict (Kubernetes-style)"""
        kind = resource_dict.get("kind")
        metadata = resource_dict.get("metadata", {})
        name = metadata.get("name")
        ns = metadata.get("namespace", namespace)
        spec = resource_dict.get("spec", {})

        # Extract labels from metadata
        clean_metadata = {
            k: v for k, v in metadata.items() if k not in ("name", "namespace")
        }

        if not kind or not name:
            raise ValueError(f"Invalid resource: missing kind or name: {resource_dict}")

        # Create resource object
        if kind == "Pod":
            resource = PodResource(name, spec, clean_metadata, ns)
        elif kind == "Service":
            resource = ServiceResource(name, spec, clean_metadata, ns)
        else:
            raise ValueError(f"Unknown resource kind: {kind}")

        # Create or update
        return self.store.create_or_update(resource)

    def apply_yaml(self, yaml_content: str, namespace: str = "default"):
        """Apply YAML resource definitions"""
        docs = yaml.safe_load_all(yaml_content)

        for doc in docs:
            if not doc:
                continue
            try:
                resource = self.apply_resource(doc, namespace)
                print(f"Applied {resource.kind}: {resource.namespace}/{resource.name}")
            except Exception as e:
                print(f"Error applying resource: {e}")

    def delete_resource(self, kind: str, name: str, namespace: str = "default"):
        """Delete a resource"""
        if self.store.delete(kind, name, namespace):
            print(f"Deleted {kind}: {namespace}/{name}")
        else:
            print(f"{kind} {namespace}/{name} not found")

    def get_resource(
        self, kind: str, name: str, namespace: str = "default"
    ) -> Optional[Resource]:
        """Get a resource"""
        return self.store.get(kind, name, namespace)

    def list_resources(self, kind: str, namespace: str = None) -> List[Resource]:
        """List resources"""
        return self.store.list(kind, namespace)


# =============================================================================
# FastAPI REST API
# =============================================================================

# Global cluster instance
cluster = OrchestratorCluster()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    cluster.start()
    yield
    cluster.stop()


app = FastAPI(
    title="Orchestrator API",
    description="Kubernetes-like orchestration system",
    version="1.0.0",
    lifespan=lifespan,
)


# Pydantic models for request/response
class ResourceMetadata(BaseModel):
    name: str
    namespace: Optional[str] = "default"
    labels: Optional[Dict[str, str]] = None

    model_config = {"extra": "allow"}


class ResourceRequest(BaseModel):
    apiVersion: str
    kind: str
    metadata: Dict[str, Any]
    spec: Dict[str, Any]

    model_config = {"extra": "allow"}


# Helper to convert resource to response dict
def resource_response(resource: Resource) -> Dict[str, Any]:
    return resource.to_dict()


def resources_list_response(kind: str, resources: List[Resource]) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": f"{kind}List",
        "items": [r.to_dict() for r in resources],
    }


# =============================================================================
# Pod Endpoints
# =============================================================================


@app.post("/api/v1/namespaces/{namespace}/pods")
async def create_pod(namespace: str, resource: ResourceRequest):
    """Create a Pod"""
    if resource.kind != "Pod":
        raise HTTPException(400, f"Expected kind Pod, got {resource.kind}")

    resource_dict = resource.model_dump()
    resource_dict["metadata"]["namespace"] = namespace

    try:
        created = cluster.apply_resource(resource_dict, namespace)
        return JSONResponse(status_code=201, content=resource_response(created))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/namespaces/{namespace}/pods")
async def list_pods(namespace: str):
    """List all Pods in a namespace"""
    pods = cluster.list_resources("Pod", namespace)
    return resources_list_response("Pod", pods)


@app.get("/api/v1/namespaces/{namespace}/pods/{name}")
async def get_pod(namespace: str, name: str):
    """Get a specific Pod"""
    pod = cluster.get_resource("Pod", name, namespace)
    if not pod:
        raise HTTPException(404, f"Pod {namespace}/{name} not found")
    return resource_response(pod)


@app.delete("/api/v1/namespaces/{namespace}/pods/{name}")
async def delete_pod(namespace: str, name: str):
    """Delete a Pod"""
    if cluster.store.delete("Pod", name, namespace):
        return {"status": "Success", "message": f"Pod {namespace}/{name} deleted"}
    raise HTTPException(404, f"Pod {namespace}/{name} not found")


@app.put("/api/v1/namespaces/{namespace}/pods/{name}")
async def update_pod(namespace: str, name: str, resource: ResourceRequest):
    """Update a Pod"""
    if resource.kind != "Pod":
        raise HTTPException(400, f"Expected kind Pod, got {resource.kind}")

    existing = cluster.get_resource("Pod", name, namespace)
    if not existing:
        raise HTTPException(404, f"Pod {namespace}/{name} not found")

    resource_dict = resource.model_dump()
    resource_dict["metadata"]["namespace"] = namespace
    resource_dict["metadata"]["name"] = name

    updated = cluster.apply_resource(resource_dict, namespace)
    return resource_response(updated)


# =============================================================================
# Service Endpoints
# =============================================================================


@app.post("/api/v1/namespaces/{namespace}/services")
async def create_service(namespace: str, resource: ResourceRequest):
    """Create a Service"""
    if resource.kind != "Service":
        raise HTTPException(400, f"Expected kind Service, got {resource.kind}")

    resource_dict = resource.model_dump()
    resource_dict["metadata"]["namespace"] = namespace

    try:
        created = cluster.apply_resource(resource_dict, namespace)
        return JSONResponse(status_code=201, content=resource_response(created))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/namespaces/{namespace}/services")
async def list_services(namespace: str):
    """List all Services in a namespace"""
    services = cluster.list_resources("Service", namespace)
    return resources_list_response("Service", services)


@app.get("/api/v1/namespaces/{namespace}/services/{name}")
async def get_service(namespace: str, name: str):
    """Get a specific Service"""
    svc = cluster.get_resource("Service", name, namespace)
    if not svc:
        raise HTTPException(404, f"Service {namespace}/{name} not found")
    return resource_response(svc)


@app.delete("/api/v1/namespaces/{namespace}/services/{name}")
async def delete_service(namespace: str, name: str):
    """Delete a Service"""
    if cluster.store.delete("Service", name, namespace):
        return {"status": "Success", "message": f"Service {namespace}/{name} deleted"}
    raise HTTPException(404, f"Service {namespace}/{name} not found")


# =============================================================================
# Messaging Endpoints (Extension to Kubernetes API)
# =============================================================================


@app.post("/api/v1/namespaces/{namespace}/pods/{name}/send")
async def send_to_pod(namespace: str, name: str, message: Dict[str, Any]):
    """Send a message to a Pod"""
    try:
        cluster.api.send_to_pod(name, message.get("data"), namespace)
        return {"status": "Success", "message": "Message sent"}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/v1/namespaces/{namespace}/pods/{name}/call")
async def call_pod(namespace: str, name: str, message: Dict[str, Any]):
    """Send a message to a Pod and wait for response"""
    try:
        future = cluster.api.send_to_pod(
            name, message.get("data"), namespace, expect_response=True
        )
        result = future.result(timeout=message.get("timeout", 30))
        return {"status": "Success", "result": result}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except TimeoutError:
        raise HTTPException(408, "Request timeout")


@app.post("/api/v1/namespaces/{namespace}/services/{name}/send")
async def send_to_service(namespace: str, name: str, message: Dict[str, Any]):
    """Send a message to a Service (load balanced to a pod)"""
    try:
        cluster.api.send_to_service(name, message.get("data"), namespace)
        return {"status": "Success", "message": "Message sent"}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/v1/namespaces/{namespace}/services/{name}/call")
async def call_service(namespace: str, name: str, message: Dict[str, Any]):
    """Send a message to a Service and wait for response"""
    try:
        future = cluster.api.send_to_service(
            name, message.get("data"), namespace, expect_response=True
        )
        result = future.result(timeout=message.get("timeout", 30))
        return {"status": "Success", "result": result}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except TimeoutError:
        raise HTTPException(408, "Request timeout")


# =============================================================================
# Health and Info Endpoints
# =============================================================================


@app.get("/healthz")
async def healthz():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/api/v1/namespaces")
async def list_namespaces():
    """List all namespaces (returns default only for this implementation)"""
    return {
        "apiVersion": "v1",
        "kind": "NamespaceList",
        "items": [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "default"}}
        ],
    }


# =============================================================================
# Main
# =============================================================================


def main():
    """Run the orchestrator server"""
    import argparse

    parser = argparse.ArgumentParser(description="Orchestrator API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=3000, help="Port to bind to")
    args = parser.parse_args()

    print(f"Starting Orchestrator API on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
