"""
Kubernetes-like orchestration system for Podman containers.

This implements:
- Resource types: Pod, Service, ReplicaSet (strict subset of Kubernetes API)
- Controllers for each resource type
- In-memory resource tree
- Inter-container communication via Podman network
- FastAPI REST API compatible with kubectl-style requests
"""

import threading
import yaml
import random
import time
import uvicorn
import subprocess
import json
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel


# =============================================================================
# Resource Definitions
# =============================================================================


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

    @property
    def ports(self) -> List[Dict[str, Any]]:
        """Extract ports from first container spec"""
        container = self.first_container
        if container:
            return container.get("ports", [])
        return []


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


@dataclass
class ReplicaSetResource(Resource):
    """ReplicaSet resource definition"""

    def __init__(
        self,
        name: str,
        spec: Dict[str, Any],
        metadata: Dict[str, Any] = None,
        namespace: str = "default",
    ):
        super().__init__(
            api_version="apps/v1",
            kind="ReplicaSet",
            name=name,
            namespace=namespace,
            spec=spec,
            metadata=metadata or {},
        )

    @property
    def replicas(self) -> int:
        return self.spec.get("replicas", 1)

    @property
    def selector(self) -> Dict[str, str]:
        return self.spec.get("selector", {})

    @property
    def template(self) -> Dict[str, Any]:
        return self.spec.get("template", {})


# =============================================================================
# Container Runtime
# =============================================================================

# Podman helpers
NETWORK_NAME = "orchestrator-network"

def run_podman(args):
    cmd = ["podman"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

def podman_inspect(container_id):
    result = run_podman(["inspect", container_id])
    if result.returncode == 0 and result.stdout.strip():
        return json.loads(result.stdout)[0]
    return None

def setup_network():
    """Create Podman network if it doesn't exist"""
    result = run_podman(["network", "ls", "--format", "{{.Name}}"])
    networks = result.stdout.strip().split("\n") if result.stdout.strip() else []

    if NETWORK_NAME not in networks:
        print(f"Creating network {NETWORK_NAME}")
        run_podman(["network", "create", NETWORK_NAME])
    else:
        print(f"Network {NETWORK_NAME} already exists")

def cleanup_network():
    """Remove network"""
    run_podman(["network", "rm", NETWORK_NAME])


class Container:
    """Running Podman container instance"""

    def __init__(
        self,
        name: str,
        image: str,
        env: Dict[str, Any],
        api_client: "OrchestratorAPI",
        labels: Dict[str, str] = None,
        namespace: str = "default",
        ports: List[Dict[str, Any]] = None,
        network_aliases: List[str] = None
    ):
        self.name = name
        self.namespace = namespace
        self.image = image
        self.env = env or {}
        self.api_client = api_client
        self.labels = labels or {}
        self.ports = ports or []
        self.network_aliases = network_aliases or []
        self.container_id = None

    def start(self):
        """Start Podman container"""
        if self.container_id:
            return

        container_name = f"{self.namespace}-{self.name}"

        # Clean up any existing container with the same name
        run_podman(["rm", "-f", container_name])

        cmd = ["run", "-d", "--name", container_name, "--network", NETWORK_NAME]

        # Add network aliases for DNS resolution (e.g., "ping", "health-service")
        for alias in self.network_aliases:
            cmd.extend(["--network-alias", alias])

        # Add port mappings (-p host:container or -p container)
        for port_spec in self.ports:
            container_port = port_spec.get("containerPort")
            host_port = port_spec.get("hostPort")

            if container_port:
                if host_port:
                    # Map host port to container port
                    cmd.extend(["-p", f"{host_port}:{container_port}"])
                else:
                    # Just expose container port (Podman assigns random host port)
                    cmd.extend(["-p", str(container_port)])

        # add environment variables
        for k, v in self.env.items():
            cmd.extend(["-e", f"{k}={v}"])

        # add labels
        for k, v in self.labels.items():
            cmd.extend(["--label", f"{k}={v}"])

        cmd.append(self.image)

        # for testing with alpine, add sleep infinity
        if "alpine" in self.image.lower():
            cmd.extend(["sleep", "infinity"])

        result = run_podman(cmd)
        if result.returncode == 0:
            self.container_id = result.stdout.strip()
            print(f"Started container {container_name}: {self.container_id[:12]}")
        else:
            print(f"Failed to start {container_name}: {result.stderr}")

    def stop(self):
        """Stop Podman container"""
        if not self.container_id:
            return

        # Use -f to force stop and remove in one command
        run_podman(["rm", "-f", self.container_id])
        self.container_id = None

    def is_running(self):
        """Check if Podman container is running"""
        if not self.container_id:
            return False

        info = podman_inspect(self.container_id)
        if info:
            return info.get("State", {}).get("Running", False)
        return False


# =============================================================================
# Resource Store
# =============================================================================


class ResourceStore:
    """In-memory resource tree"""

    def __init__(self):
        self.resources: Dict[str, Dict[str, Dict[str, Resource]]] = {
            "Pod": {},  # namespace -> name -> resource
            "Service": {},
            "ReplicaSet": {},
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

            # Create containers for new pods and check health of existing ones
            for key, pod in desired_pods.items():
                if key not in self.containers:
                    print(f"Starting pod: {key}")
                    # Add network alias as pod name (for DNS: http://ping:5000)
                    network_aliases = [pod.name]

                    container = Container(
                        name=pod.name,
                        namespace=pod.namespace,
                        image=pod.image,
                        env=pod.env,
                        api_client=self.api_client,
                        labels=pod.labels,
                        ports=pod.ports,
                        network_aliases=network_aliases,
                    )
                    container.start()
                    self.containers[key] = container
                    # Update pod status
                    pod.status = {"phase": "Running", "containerID": container.container_id}
                else:
                    # Check health of existing containers
                    container = self.containers[key]
                    if not container.is_running():
                        print(f"Pod {key} died, restarting...")
                        container.stop()
                        container.start()
                        pod.status = {"phase": "Running", "containerID": container.container_id}

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
    """Controller for Service resources - creates load balancer containers"""

    def __init__(self, store: ResourceStore, pod_controller: PodController):
        super().__init__(store)
        self.pod_controller = pod_controller
        self.lb_containers: Dict[str, Container] = {}  # "namespace/service-name" -> Container
        self.lock = threading.RLock()

    def _service_key(self, namespace: str, name: str) -> str:
        return f"{namespace}/{name}"

    def reconcile(self):
        """Create/update load balancer containers for services"""
        with self.lock:
            # Get all services
            desired_services = {}
            for service in self.store.list("Service"):
                key = self._service_key(service.namespace, service.name)
                desired_services[key] = service

            # Stop and remove LB containers for deleted services
            for key in list(self.lb_containers.keys()):
                if key not in desired_services:
                    print(f"Stopping service LB: {key}")
                    self.lb_containers[key].stop()
                    del self.lb_containers[key]

            # Create or update LB containers for services
            for key, service in desired_services.items():
                # Check if service has backend pods
                backend_containers = self.pod_controller.get_containers_by_labels(
                    service.selector, service.namespace
                )

                if not backend_containers:
                    print(f"Warning: Service {service.name} selector {service.selector} matches no pods")
                    # Remove LB if no backends exist
                    if key in self.lb_containers:
                        self.lb_containers[key].stop()
                        del self.lb_containers[key]
                    continue

                # Create LB container if it doesn't exist
                if key not in self.lb_containers:
                    self._create_lb_container(service, backend_containers)
                else:
                    # Check if LB is still running
                    lb_container = self.lb_containers[key]
                    if not lb_container.is_running():
                        print(f"Service LB {key} died, restarting...")
                        lb_container.stop()
                        self._create_lb_container(service, backend_containers)

    def _create_lb_container(self, service: ServiceResource, backend_containers: List[Container]):
        """Create a load balancer container for a service"""
        key = self._service_key(service.namespace, service.name)

        if not service.ports:
            print(f"Warning: Service {service.name} has no ports defined")
            return

        # Use first port spec for now (most services have one port)
        port_spec = service.ports[0]
        service_port = port_spec.get("port")
        target_port = port_spec.get("targetPort", service_port)

        # Build backend list using pod names (DNS-resolvable): pod1:8080,pod2:8080,...
        backends = []
        for container in backend_containers:
            # Use pod name (network alias) instead of Podman container name
            backends.append(f"{container.name}:{target_port}")

        if not backends:
            return

        # Create LB container with environment variables
        env = {
            "SERVICE_NAME": service.name,
            "SERVICE_PORT": str(service_port),
            "BACKENDS": ",".join(backends),  # comma-separated list
        }

        lb_name = f"lb-{service.name}"
        labels = {"component": "load-balancer", "service": service.name}

        # Network alias = service name (for DNS: http://health-service:2000)
        network_aliases = [service.name]

        # Port mapping: expose service port to host and for in-cluster access
        ports = [
            {"containerPort": service_port, "hostPort": service_port}
        ]

        lb_container = Container(
            name=lb_name,
            namespace=service.namespace,
            image="orchestrator-lb",  # Custom LB image (to be built)
            env=env,
            api_client=self.pod_controller.api_client,
            labels=labels,
            ports=ports,
            network_aliases=network_aliases,
        )

        lb_container.start()
        self.lb_containers[key] = lb_container
        print(f"Created LB for service {key} (port {service_port}) with backends: {backends}")


class ReplicaSetController(Controller):
    """Controller for ReplicaSet resources"""

    def __init__(self, store: ResourceStore):
        super().__init__(store)
        self.lock = threading.RLock()

    def reconcile(self):
        """Ensure desired number of pod replicas exist"""
        with self.lock:
            for replicaset in self.store.list("ReplicaSet"):
                self._reconcile_replicaset(replicaset)

    def _reconcile_replicaset(self, replicaset: ReplicaSetResource):
        """Reconcile a single ReplicaSet"""
        desired_replicas = replicaset.replicas
        selector = replicaset.selector
        namespace = replicaset.namespace

        # Find all pods owned by this ReplicaSet
        owned_pods = self._find_owned_pods(replicaset)
        actual_replicas = len(owned_pods)

        if actual_replicas < desired_replicas:
            # CREATE new pods
            num_to_create = desired_replicas - actual_replicas
            for i in range(num_to_create):
                self._create_pod_from_template(replicaset)
                print(f"ReplicaSet {namespace}/{replicaset.name}: created pod ({actual_replicas + i + 1}/{desired_replicas})")

        elif actual_replicas > desired_replicas:
            # DELETE excess pods
            num_to_delete = actual_replicas - desired_replicas
            for pod in owned_pods[:num_to_delete]:
                self.store.delete("Pod", pod.name, namespace)
                print(f"ReplicaSet {namespace}/{replicaset.name}: deleted pod {pod.name} ({actual_replicas - num_to_delete}/{desired_replicas})")

        # UPDATE ReplicaSet status
        replicaset.status = {
            "replicas": len(self._find_owned_pods(replicaset)),
            "readyReplicas": len(self._find_owned_pods(replicaset)),
        }

    def _find_owned_pods(self, replicaset: ReplicaSetResource) -> List[PodResource]:
        """Find pods owned by this ReplicaSet"""
        all_pods = self.store.list("Pod", replicaset.namespace)
        owned_pods = []

        for pod in all_pods:
            # Check if pod name starts with replicaset name
            if pod.name.startswith(f"{replicaset.name}-"):
                # Check if pod labels match selector
                matches = all(pod.labels.get(k) == v for k, v in replicaset.selector.items())
                if matches:
                    owned_pods.append(pod)

        return owned_pods

    def _create_pod_from_template(self, replicaset: ReplicaSetResource):
        """Create a new pod from ReplicaSet template"""
        import string

        # Generate unique name: <replicaset-name>-<random-5-chars>
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
        pod_name = f"{replicaset.name}-{suffix}"

        # Extract pod spec from template
        template = replicaset.template
        pod_spec = template.get("spec", {})
        template_metadata = template.get("metadata", {})

        # Merge selector labels with template metadata
        pod_metadata = {**template_metadata, **replicaset.selector}

        # Create pod resource
        pod = PodResource(
            name=pod_name,
            spec=pod_spec,
            metadata=pod_metadata,
            namespace=replicaset.namespace,
        )

        # Add to store
        self.store.create_or_update(pod)


# =============================================================================
# API
# =============================================================================


class OrchestratorAPI:
    """
    API for interacting with containers.

    Note: With Podman-based containers using HTTP workers,
    inter-container communication happens via HTTP requests
    to service names (e.g., http://health-service:2000)
    rather than in-process message queues.
    """

    def __init__(self, pod_controller: PodController, store: ResourceStore):
        self.pod_controller = pod_controller
        self.store = store


# =============================================================================
# Cluster
# =============================================================================


class OrchestratorCluster:
    """Main cluster orchestrator"""

    def __init__(self):
        self.store = ResourceStore()
        self.pod_controller = None
        self.service_controller = None
        self.replicaset_controller = None
        self.api = None
        self._started = False

    def start(self):
        """Start the cluster"""
        if self._started:
            return

        # Setup Podman network
        setup_network()

        # Initialize API
        self.pod_controller = PodController(self.store, None)
        self.api = OrchestratorAPI(self.pod_controller, self.store)
        self.pod_controller.api_client = self.api

        # Initialize controllers
        self.service_controller = ServiceController(self.store, self.pod_controller)
        self.replicaset_controller = ReplicaSetController(self.store)

        # Start controllers
        self.pod_controller.start()
        self.service_controller.start()
        self.replicaset_controller.start()

        self._started = True
        print("Orchestrator cluster started")

    def stop(self):
        """Stop the cluster"""
        if self.pod_controller:
            self.pod_controller.stop()
        if self.service_controller:
            self.service_controller.stop()
        if self.replicaset_controller:
            self.replicaset_controller.stop()
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
        elif kind == "ReplicaSet":
            resource = ReplicaSetResource(name, spec, clean_metadata, ns)
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
# ReplicaSet Endpoints
# =============================================================================


@app.post("/api/apps/v1/namespaces/{namespace}/replicasets")
async def create_replicaset(namespace: str, resource: ResourceRequest):
    """Create a ReplicaSet"""
    if resource.kind != "ReplicaSet":
        raise HTTPException(400, f"Expected kind ReplicaSet, got {resource.kind}")

    resource_dict = resource.model_dump()
    resource_dict["metadata"]["namespace"] = namespace

    try:
        created = cluster.apply_resource(resource_dict, namespace)
        return JSONResponse(status_code=201, content=resource_response(created))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/v1/namespaces/{namespace}/replicasets")
async def list_replicasets(namespace: str):
    """List all ReplicaSets in a namespace"""
    replicasets = cluster.list_resources("ReplicaSet", namespace)
    return resources_list_response("ReplicaSet", replicasets)


@app.get("/api/apps/v1/namespaces/{namespace}/replicasets/{name}")
async def get_replicaset(namespace: str, name: str):
    """Get a specific ReplicaSet"""
    rs = cluster.get_resource("ReplicaSet", name, namespace)
    if not rs:
        raise HTTPException(404, f"ReplicaSet {namespace}/{name} not found")
    return resource_response(rs)


@app.delete("/api/apps/v1/namespaces/{namespace}/replicasets/{name}")
async def delete_replicaset(namespace: str, name: str):
    """Delete a ReplicaSet (cascade delete owned pods)"""
    rs = cluster.get_resource("ReplicaSet", name, namespace)
    if not rs:
        raise HTTPException(404, f"ReplicaSet {namespace}/{name} not found")

    # Find and delete owned pods
    if cluster.replicaset_controller:
        owned_pods = cluster.replicaset_controller._find_owned_pods(rs)
        for pod in owned_pods:
            cluster.store.delete("Pod", pod.name, namespace)

    # Delete the ReplicaSet
    if cluster.store.delete("ReplicaSet", name, namespace):
        return {"status": "Success", "message": f"ReplicaSet {namespace}/{name} deleted"}

    raise HTTPException(404, f"ReplicaSet {namespace}/{name} not found")


@app.put("/api/apps/v1/namespaces/{namespace}/replicasets/{name}")
async def update_replicaset(namespace: str, name: str, resource: ResourceRequest):
    """Update a ReplicaSet"""
    if resource.kind != "ReplicaSet":
        raise HTTPException(400, f"Expected kind ReplicaSet, got {resource.kind}")

    existing = cluster.get_resource("ReplicaSet", name, namespace)
    if not existing:
        raise HTTPException(404, f"ReplicaSet {namespace}/{name} not found")

    resource_dict = resource.model_dump()
    resource_dict["metadata"]["namespace"] = namespace
    resource_dict["metadata"]["name"] = name

    updated = cluster.apply_resource(resource_dict, namespace)
    return resource_response(updated)


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
