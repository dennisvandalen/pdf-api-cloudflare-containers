#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BASE_URL:-"https://pdf-api-cloudflare-containers.vandalen.workers.dev"}
IMAGE=${1:-"demo_files/30ce5af2-9a42-4d94-a04d-da51601821a6-935461.png"}
OUT_DIR="demo_files"

echo "=== Testing /spot-color-layer ==="
curl -sS -X POST "$BASE_URL/spot-color-layer" \
  -F "file=@$IMAGE" \
  -o "$OUT_DIR/test-spot-only.pdf" \
  -w "HTTP %{http_code}\n"
echo "Saved to $OUT_DIR/test-spot-only.pdf"

echo ""
echo "=== Testing /image-with-spot-color ==="
curl -sS -X POST "$BASE_URL/image-with-spot-color" \
  -F "file=@$IMAGE" \
  -o "$OUT_DIR/test-image-with-spot.pdf" \
  -w "HTTP %{http_code}\n"
echo "Saved to $OUT_DIR/test-image-with-spot.pdf"

echo ""
echo "=== Inspecting outputs ==="
python3 scripts/dissect-pdf.py "$OUT_DIR/test-spot-only.pdf"
echo ""
python3 scripts/dissect-pdf.py "$OUT_DIR/test-image-with-spot.pdf"
