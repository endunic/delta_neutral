# ---------------------------------------------------------------------------
# Dockerfile for the delta_neutral Deribit testnet trading bot
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Avoid writing __pycache__ into the image and disable buffering for logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install minimal system build dependencies (needed by some wheels)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first so this layer is cached unless the
# requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application source code
COPY . .

# This is a long-running background worker (trading/scanner pipeline),
# not an HTTP web service, so we simply launch the main entry point.
CMD ["python", "main.py"]
