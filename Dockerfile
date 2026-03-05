FROM python:3.10-slim

WORKDIR /app

# Install dependencies needed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Set default env variables (optional, as they are typically provided by docker-compose/.env)
ENV SYNC_INTERVAL=900

# Default command: run sync in a loop
CMD ["python", "sync.py", "sync", "--loop", "--interval", "$SYNC_INTERVAL"]
