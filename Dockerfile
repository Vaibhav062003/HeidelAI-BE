# using a lighweight base image

FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-libmysqlclient-dev \
    libpq-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Stage 2 - Final slim image
FROM python:3.12-slim

WORKDIR /app

# CHANGE: Install runtime libraries only, not dev tools
# libpq5 = runtime for Postgres
# default-libmysqlclient-dev = unfortunately often needed for MySQL on Debian, 
# but we can try to rely on the shared libs if possible. 
# For safety, we keep the MySQL dev lib if the runtime link is tricky, 
# but definitely swap libpq-dev for libpq5.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    pkg-config \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

RUN addgroup --system app && adduser --system --ingroup app app

# Copy application code
COPY --chown=app:app . .

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
USER app
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
