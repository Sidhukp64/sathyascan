# SathyaScan - Public Fact-Checking Web Platform
# Root Dockerfile for automated cloud deployment (Render, Railway, Fly.io, Cloud Run, VPS)

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies if needed (e.g. for curl healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application, database migrations, and public web dashboard
COPY backend/app ./app
COPY backend/alembic.ini .
COPY backend/migrations ./migrations
COPY dashboard ./dashboard

EXPOSE 8000

ENV PORT=8000
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
