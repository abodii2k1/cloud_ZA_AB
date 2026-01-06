"""
Worker modules for the Orchestrator demo.
These functions can be deployed as containers (pods).

Each worker function receives:
- input_queue: Queue to receive messages
- api_client: OrchestratorAPI instance for inter-pod communication
- **env: Environment variables passed via pod spec
"""

import time
import random


def health(input_queue, api_client, **env):
    """
    Health check worker - returns True for any request.

    This is a simple worker that responds to health check requests.
    """
    print("Health worker started")

    while True:
        item = input_queue.get()

        # None is the signal to stop
        if item is None:
            break

        # Check if this is a request-response pattern
        if isinstance(item, tuple) and len(item) == 2:
            value, future = item
            print(f"Health check request received: {value}")
            future.set_result(True)
        else:
            print(f"Health worker received: {item}")

    print("Health worker stopped")


def ping(input_queue, api_client, HEALTH_SERVICE=None, **env):
    """
    Ping worker - calls health service every second and prints result.

    Args:
        input_queue: Queue to receive control messages
        api_client: API client for inter-pod communication
        HEALTH_SERVICE: Service reference like "health-service:2000"
    """
    print(f"Ping worker started, targeting: {HEALTH_SERVICE}")

    if not HEALTH_SERVICE:
        print("Warning: HEALTH_SERVICE not configured")
        return

    # Parse service reference
    if ":" in HEALTH_SERVICE:
        service_name, port = HEALTH_SERVICE.rsplit(":", 1)
    else:
        service_name = HEALTH_SERVICE
        port = None

    while True:
        try:
            # Check for stop signal
            try:
                item = input_queue.get(timeout=1)
                if item is None:
                    break
            except:
                pass

            # Call health service
            try:
                future = api_client.send_to_service(service_name, "ping", expect_response=True)
                result = future.result(timeout=5)
                print(f"Ping -> {service_name}: {result}")
            except Exception as e:
                print(f"Ping -> {service_name}: ERROR - {e}")

        except Exception as e:
            print(f"Ping worker error: {e}")

    print("Ping worker stopped")


def echo_worker(input_queue, api_client, prefix="ECHO", **env):
    """
    Simple echo worker that responds to messages.

    Args:
        input_queue: Queue to receive messages
        api_client: API client for inter-pod communication
        prefix: Prefix to add to echoed messages
    """
    print(f"Echo worker started with prefix: {prefix}")

    while True:
        item = input_queue.get()

        # None is the signal to stop
        if item is None:
            break

        # Check if this is a request-response pattern
        if isinstance(item, tuple) and len(item) == 2:
            value, future = item
            response = f"{prefix}: {value}"
            print(f"Echo worker processing request: {value} -> {response}")
            future.set_result(response)
        else:
            # Just a fire-and-forget message
            print(f"Echo worker received: {prefix}: {item}")

    print("Echo worker stopped")


def processor_worker(input_queue, api_client, operation="uppercase", forward_to=None, **env):
    """
    Worker that processes strings and optionally forwards results.

    Args:
        input_queue: Queue to receive messages
        api_client: API client for inter-pod communication
        operation: Operation to perform (uppercase, lowercase, reverse)
        forward_to: Optional pod/service name to forward results to
    """
    print(f"Processor worker started with operation: {operation}")

    while True:
        item = input_queue.get()

        if item is None:
            break

        # Handle request-response pattern
        if isinstance(item, tuple) and len(item) == 2:
            value, future = item
        else:
            value = item
            future = None

        # Process the value
        try:
            if operation == "uppercase":
                result = str(value).upper()
            elif operation == "lowercase":
                result = str(value).lower()
            elif operation == "reverse":
                result = str(value)[::-1]
            else:
                result = str(value)

            print(f"Processor worker: {value} -> {result}")

            # Send response if expected
            if future:
                future.set_result(result)

            # Forward to another pod/service if configured
            if forward_to:
                try:
                    api_client.send_to_pod(forward_to, result)
                except:
                    try:
                        api_client.send_to_service(forward_to, result)
                    except Exception as e:
                        print(f"Failed to forward to {forward_to}: {e}")

        except Exception as e:
            print(f"Processor worker error: {e}")
            if future:
                future.set_exception(e)

    print("Processor worker stopped")


def aggregator_worker(input_queue, api_client, window_size=5, **env):
    """
    Worker that aggregates messages and reports statistics.

    Args:
        input_queue: Queue to receive messages
        api_client: API client for inter-pod communication
        window_size: Number of messages to aggregate before reporting
    """
    # Convert to int if string
    if isinstance(window_size, str):
        window_size = int(window_size)
        
    print(f"Aggregator worker started with window size: {window_size}")

    messages = []
    futures = []

    while True:
        item = input_queue.get()

        if item is None:
            for future in futures:
                future.set_result(None)
            break

        # Handle request-response pattern
        if isinstance(item, tuple) and len(item) == 2:
            value, future = item
            futures.append(future)
        else:
            value = item
            future = None

        messages.append(value)
        print(f"Aggregator received: {value} (count: {len(messages)})")

        # Report when window is full
        if len(messages) >= window_size:
            report = {
                "count": len(messages),
                "messages": messages.copy(),
                "sample": messages[0] if messages else None,
            }
            print(f"Aggregator report: {report}")

            for f in futures:
                f.set_result(report)

            messages.clear()
            futures.clear()

    print("Aggregator worker stopped")


def generator_worker(input_queue, api_client, target=None, interval=2, count=10, **env):
    """
    Worker that generates messages and sends them to a target.

    Args:
        input_queue: Queue to receive messages (for control)
        api_client: API client for inter-pod communication
        target: Pod or service name to send messages to
        interval: Seconds between messages
        count: Number of messages to generate
    """
    # Convert to int/float if strings
    if isinstance(interval, str):
        interval = float(interval)
    if isinstance(count, str):
        count = int(count)
        
    print(f"Generator worker started, will send {count} messages to {target}")

    for i in range(count):
        message = f"message-{i}-{random.randint(1000, 9999)}"

        try:
            print(f"Generator sending: {message}")
            try:
                api_client.send_to_service(target, message)
            except:
                api_client.send_to_pod(target, message)
        except Exception as e:
            print(f"Generator failed to send: {e}")

        time.sleep(interval)

        # Check for stop signal
        try:
            item = input_queue.get(timeout=0.1)
            if item is None:
                break
        except:
            pass

    print("Generator worker finished")


def calculator_worker(input_queue, api_client, **env):
    """
    Worker that performs calculations on request-response basis.

    Args:
        input_queue: Queue to receive calculation requests
        api_client: API client for inter-pod communication
    """
    print("Calculator worker started")

    while True:
        item = input_queue.get()

        if item is None:
            break

        # Must be request-response pattern
        if not isinstance(item, tuple) or len(item) != 2:
            print("Calculator: ignoring non-request message")
            continue

        request, future = item

        try:
            # Request should be dict with 'operation' and 'operands'
            operation = request.get("operation")
            operands = request.get("operands", [])

            if operation == "sum":
                result = sum(operands)
            elif operation == "product":
                result = 1
                for x in operands:
                    result *= x
            elif operation == "average":
                result = sum(operands) / len(operands) if operands else 0
            else:
                result = None

            print(f"Calculator: {operation}({operands}) = {result}")
            future.set_result(result)

        except Exception as e:
            print(f"Calculator error: {e}")
            future.set_exception(e)

    print("Calculator worker stopped")
