# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps (optional but handy for debugging)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .
COPY templates/ /app/templates/

# Expose port
EXPOSE 5000

# Start with Gunicorn: 4 workers, each 8 threads (tweak to taste)
# "--timeout 120" prevents workers from being killed too quickly on slow upstreams
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "8", "--timeout", "120", "--graceful-timeout", "30", "-b", "0.0.0.0:5000", "app:app"]
