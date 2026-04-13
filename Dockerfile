# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAGICK_MEMORY_LIMIT=3GiB \
    MAGICK_MAP_LIMIT=3GiB \
    MAGICK_DISK_LIMIT=3GiB

WORKDIR /app

# System dependencies for PDF pipeline (ghostscript built from source below)
RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    mupdf-tools \
    libcairo2-dev \
    libffi-dev \
    libjpeg-dev \
    libpng-dev \
    wget \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# Build Ghostscript 9.55.0 from source (GS 10's new PDF interpreter drops vector elements)
RUN wget -q https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs9550/ghostscript-9.55.0.tar.gz \
  && tar xzf ghostscript-9.55.0.tar.gz \
  && cd ghostscript-9.55.0 \
  && ./configure --quiet --without-x \
  && make -j$(nproc) -s \
  && make install -s \
  && cd / && rm -rf ghostscript-9.55.0 ghostscript-9.55.0.tar.gz

# Python deps
COPY container_src/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# App source and test data
COPY container_src/ /app/
COPY container_src/data/ /app/data/

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]