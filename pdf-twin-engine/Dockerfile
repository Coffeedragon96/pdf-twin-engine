# ---------------------------------------------------------------
# Stage 1: Builder - install dependencies
# ---------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies required by PyMuPDF and Shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------
# Stage 2: Runtime - lean production image
# ---------------------------------------------------------------
FROM python:3.11-slim

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Runtime system libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Copy application source
COPY . .

CMD ["python", "-m", "app.worker.consumer"]