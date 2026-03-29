# PDF API (Cloudflare Containers)

Python FastAPI service running inside a Cloudflare Container that provides PDF manipulation tools — SVG overlay with named spot colors, flattening, cropping, overprint previews, and separation rendering.

A Cloudflare Worker (Hono) proxies all requests to a singleton container instance.

## Architecture

```
Client → Cloudflare Worker (Hono/TypeScript) → Durable Object Container (Python/FastAPI)
```

- **Worker** (`src/index.ts`): Hono app that proxies every route to a singleton container Durable Object. Keeps the container warm for 10 minutes after the last request.
- **Container** (`container_src/app.py`): FastAPI app with ImageMagick, Ghostscript, CairoSVG, pikepdf, pypdf, and PDFium.
- **Dockerfile**: Python 3.12-slim with system deps (ImageMagick, Ghostscript, Cairo) and Python packages.

## Project Structure

```
├── src/index.ts              # Cloudflare Worker entry point (Hono proxy)
├── container_src/
│   ├── app.py                # FastAPI application with all endpoints
│   ├── requirements.txt      # Python dependencies
│   ├── assets/               # Bundled sample files (base image, SVG overlay)
│   └── data/                 # Test data (SVGs, images)
├── Dockerfile                # Container image definition
├── wrangler.jsonc            # Wrangler / Cloudflare Containers config
├── package.json              # Node dependencies (Wrangler, Hono, @cloudflare/containers)
└── scripts/                  # Helper scripts
```

## API Endpoints

All POST endpoints accept input as either a container path, an HTTP(S) URL, or the special token `"@sample"` (for bundled test files). Most endpoints also have a `/upload` variant that accepts multipart file uploads.

### `GET /health`

Returns ImageMagick version, policy checks (detects restrictive PDF policies), and overall service status.

### `POST /generate`

Overlays an SVG on a base image and produces a print-ready PDF with a named spot color (Separation color space) and overprint enabled.

```json
{
  "base_image": "/data/130960.png",
  "svg_overlay": "/data/star_overlay.svg",
  "spot_name": "gold",
  "allowed_classes": ["stars", "constLines1", "constLines2", "constLines3"],
  "placement": {
    "left": 89,
    "top": 152,
    "width": 1033,
    "units": "pt",
    "origin": "top-left"
  }
}
```

- `allowed_classes` (optional): filter SVG elements by CSS class; omit to use the full SVG.
- `placement` (optional): position and scale the overlay. Supports `left`/`top` or `x`/`y`, `px` or `pt` units, `top-left` or `bottom-left` origin. Omit to stretch-fit.

Pipeline: ImageMagick converts base image to CMYK PDF → CairoSVG renders SVG to PDF → pikepdf rewrites color operators to the named spot color with overprint → pypdf merges overlay onto base.

### `POST /flatten`

Rasterizes all pages of a PDF using PDFium and reassembles into a new PDF.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "dpi": 300
}
```

Upload variant:
```bash
curl -X POST /flatten/upload -F "file=@input.pdf" -F "dpi=300" --output flattened.pdf
```

### `POST /page-dimensions`

Returns page dimensions (mm and pt) for every page in a PDF.

```json
{
  "input_pdf": "/path/to/file.pdf"
}
```

Response:
```json
{
  "page_count": 1,
  "pages": [
    { "index": 0, "width_mm": 210.0, "height_mm": 297.0, "width_pt": 595.276, "height_pt": 841.89 }
  ]
}
```

### `POST /crop-fit-mm`

Center-crops each page to the specified dimensions in millimeters. Pages smaller than the target are left unchanged.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "width_mm": 210.0,
  "height_mm": 297.0
}
```

### `POST /overprint-preview`

Renders a composite preview PNG simulating overprint — shows how spot colors interact with the CMYK base by compositing Ghostscript separation channels with correct tint colors at 50% opacity.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "page": 0,
  "dpi": 150
}
```

How it works:
1. Detects spot colors using pikepdf
2. Uses Ghostscript `tiffsep` to extract CMYK and spot separations
3. Reconstructs RGB from CMYK separations using numpy
4. Overlays spot colors as semi-transparent colored layers
5. Resizes to 500x500px max

### `POST /render-cmyk-only`

Renders only CMYK process color channels (excluding spot colors). Returns a PNG.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "page": 0,
  "max_size": 500
}
```

### `POST /render-spots-only`

Renders only the spot color separation layers as a semi-transparent overlay PNG.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "page": 0,
  "max_size": 500
}
```

### `POST /quick-preview`

Fast preview — strips spot color content from the PDF, then renders with PDFium. Lighter than the Ghostscript-based overprint preview.

```json
{
  "input_pdf": "/path/to/file.pdf",
  "page": 0,
  "max_size": 500
}
```

## Quick Test

```bash
./scripts/test-generate.sh
```

With environment overrides:
```bash
ENDPOINT=https://pdf-api-cloudflare-containers.vandalen.workers.dev/generate \
OUT=final.pdf \
./scripts/test-generate.sh
```

## Development

```bash
npm install
npm run dev       # local dev with Wrangler
npm run deploy    # deploy to Cloudflare
```

## Container Configuration

Configured in `wrangler.jsonc`:
- Instance type: `standard-4`
- Max instances: 5
- Sleep after: 10 minutes of inactivity

Container image:
- Base: `python:3.12-slim`
- System packages: `imagemagick`, `ghostscript`, Cairo, libjpeg/png
- Python packages: `fastapi`, `uvicorn`, `cairosvg`, `pikepdf`, `pypdf`, `pypdfium2`, `Pillow`, `numpy`, `python-multipart`
- ImageMagick resource limits: 3 GiB memory/map/disk (configurable in Dockerfile)
