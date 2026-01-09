FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir flask requests

# Copy load balancer script
COPY lb_proxy.py /app/

# Expose default port
EXPOSE 8080

# Run the load balancer
CMD ["python", "lb_proxy.py"]
