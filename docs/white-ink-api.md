# White Ink Spot Color API

Generate print-ready PDFs with a white ink spot color layer from an image's alpha channel.

## Endpoint

`POST /v1/print/white-ink`

**Base URL:** `https://pdf-api-cloudflare-containers.vandalen.workers.dev`

## Request

Multipart form upload.

| Field | Type | Default | Description |
|---|---|---|---|
| `image` | PNG file | required | PNG with alpha channel. Non-transparent pixels → white ink. |
| `include_image` | bool | `true` | Include base RGB image under the ink layer |
| `spot_name` | string | `"white"` | Spot separation name (must match print provider) |
| `dpi` | int | `300` | Output resolution |

## Response

- `200 OK` — `application/pdf` binary (print-ready)
- Headers:
  - `X-Spot-Name` — the spot color name used
  - `X-Includes-Base-Image` — `true` / `false`
- `500` — `{ "detail": "..." }` on error

## Input requirements

- **Format:** PNG (RGBA). Other formats work but alpha channel is required for ink masking.
- **Transparency matters:** the alpha channel drives where white ink is applied — fully opaque pixels get 100% ink, alpha 128 gets 50% tint, alpha 0 gets none.
- **Size:** page dimensions = `image_px / dpi` inches. A 2953×3425 image at 300 dpi = 9.84"×11.42" (≈250×290mm).

## Output structure

The returned PDF contains:
- RGB base image with SMask (if `include_image=true`)
- Separation color space named per `spot_name` (CMYK fallback `[0, 0.9, 0, 0]`)
- Spot color image (alpha channel as tint) on top with overprint enabled
- OCG layers so viewers can toggle the ink layer independently

## Examples

### cURL — with base image (default)

```bash
curl -X POST https://pdf-api-cloudflare-containers.vandalen.workers.dev/v1/print/white-ink \
  -F "image=@design.png" \
  -o white-ink.pdf
```

### cURL — spot color only (no base image)

The PNG is still required — its alpha channel determines where the ink goes — but the RGB image is not included in the output.

```bash
curl -X POST https://pdf-api-cloudflare-containers.vandalen.workers.dev/v1/print/white-ink \
  -F "image=@design.png" \
  -F "include_image=false" \
  -o white-ink-only.pdf
```

### Node.js / Cloudflare Worker

```javascript
const form = new FormData();
form.append("image", imageBlob, "design.png");

const resp = await fetch(
  "https://pdf-api-cloudflare-containers.vandalen.workers.dev/v1/print/white-ink",
  { method: "POST", body: form }
);
const pdf = await resp.arrayBuffer();
```

### PHP

```php
$ch = curl_init();
curl_setopt($ch, CURLOPT_URL, "https://pdf-api-cloudflare-containers.vandalen.workers.dev/v1/print/white-ink");
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_POSTFIELDS, [
  "image" => new CURLFile("design.png"),
]);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
$pdf = curl_exec($ch);
```

## Typical flow in an app

1. User uploads or designs an image with transparency (e.g. artwork with cut-out background)
2. App POSTs the PNG to `/v1/print/white-ink`
3. Store the returned PDF in object storage (S3/R2)
4. Send PDF to print provider via their order API

## Verification

Use `POST /debug` with the returned PDF to validate:
- Confirms spot color name and CMYK fallback
- Shows overprint simulation preview (via Ghostscript tiffsep)
- Detects bad transparency handling via ink analysis heatmap
- Previews the base image with SMask applied (what prints under the spot layer)

Web UI available at `/debug`.
