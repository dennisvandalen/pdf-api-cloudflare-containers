#!/usr/bin/env bash
set -euo pipefail

ENDPOINT=${ENDPOINT:-"https://pdf-api-cloudflare-containers.vandalen.workers.dev/generate"}
OUT=${OUT:-"final.pdf"}

curl -sS -X POST "$ENDPOINT" \
  -H 'Content-Type: application/json' \
  -d '{
    "base_image": "/data/130960.png",
    "svg_overlay": "/data/star_overlay.svg",
    "spot_name": "gold",
    "allowed_classes": ["stars","constLines1","constLines2","constLines3"],
    "placement": {"left": 89, "top": 152, "width": 1033, "units": "pt", "origin": "top-left"}
  }' \
  -o "$OUT"

echo "Saved to $OUT"



