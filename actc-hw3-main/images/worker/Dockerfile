FROM python:3.11-slim

WORKDIR /app

# Install Flask and requests
RUN pip install --no-cache-dir flask requests

# Copy worker server
COPY http_worker.py /app/

# Expose port
EXPOSE 8080

# Run the worker server
CMD ["python", "http_worker.py"]
