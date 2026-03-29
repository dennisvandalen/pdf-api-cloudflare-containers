# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAGICK_MEMORY_LIMIT=3GiB \
    MAGICK_MAP_LIMIT=3GiB \
    MAGICK_DISK_LIMIT=3GiB

WORKDIR /app

# System dependencies for PDF pipeline
RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    ghostscript \
    libcairo2-dev \
    libffi-dev \
    libjpeg-dev \
    libpng-dev \
  && rm -rf /var/lib/apt/lists/*

# Python deps
COPY container_src/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# App source and test data
COPY container_src/ /app/
COPY container_src/data/ /app/data/

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]