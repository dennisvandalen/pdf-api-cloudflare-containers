# Spot Layers API

`POST /v1/print/spot-layers` — a print and its finishes as one PDF, each
finish a spot colour of its own, overprinting the print.

It is what foil is ordered with. The printmijnstad renderer draws a design
twice: the print as a PNG, and each finish as its own PDF, printed by a
browser and therefore vector, text included. This endpoint turns that into
the file a printer gets.

## Request

Multipart, because the files travel as files. Open: set the worker secret
`SPOT_LAYERS_SECRET` and it is required as
`Authorization: Bearer <SPOT_LAYERS_SECRET>` from that moment on.

| Field | Content |
| --- | --- |
| `spec` | JSON, below |
| `base` | The print, PNG in RGB. Optional — without it the layers come back on their own, which is what a proof is |
| `layer.{name}` | One PDF per layer, as printed by the browser, one page each |

```json
{
  "page": {"width_mm": 297, "height_mm": 420, "bleed_mm": 0},
  "layers": [
    {"name": "foil-gold", "spot": "Gold", "fallback_cmyk": [0, 0.2, 0.6, 0.2]},
    {"name": "foil-silver", "spot": "Silver", "fallback_cmyk": [0, 0, 0, 0.35]}
  ],
  "output": {"ocg": true}
}
```

- `page` is the trimmed size; `bleed_mm` is added on every side, and the
  TrimBox is set to the trim inside it.
- `spot` is the separation's name, as the supplier calls the ink.
- `fallback_cmyk` is what a viewer without that ink shows instead. It is a
  hint, not a colour promise.
- `output.ocg` gives each layer a group that can be switched off in a viewer.
  On by default.

```
curl -X POST https://<worker>/v1/print/spot-layers \
  -F 'spec={"page":{"width_mm":297,"height_mm":420},"layers":[{"name":"foil-gold","spot":"Gold","fallback_cmyk":[0,0.2,0.6,0.2]}]}' \
  -F base=@print.png \
  -F layer.foil-gold=@foil-gold.pdf \
  -o print.pdf
```

## Response

`application/pdf`, one page: the print as an RGB image at page size, and
above it each layer in its separation at tint 1.0 with overprint on
(`/OP`, `/op`, `/OPM 1`). `X-Spot-Layers` lists the layers that went in.

## What it refuses (422)

Foil is cut from a sheet and pressed on, so a layer is line work or nothing:

- a raster or inline image in a layer;
- an ExtGState below full opacity, or carrying a soft mask;
- a layer PDF of more than one page;
- a page without a `width_mm` and a `height_mm`, or a layer without a file.

The message names what was found (`/X4 is a raster image`), because the point
is to say which element cannot be made.

## Two things worth knowing

**Every paint in a layer is rewritten, not converted.** The browser draws in
RGB because that is all it can do, and what colour a foil layer was drawn in
means nothing — it is one ink at full strength or it is not there. The
content streams are parsed into operands and operators (a pattern like
"number then `g`" would also hit the `g` of a word in a text string), form
XObjects are followed because Chrome nests most of a page in one, and Type 3
charprocs are followed because Chrome embeds a variable font's glyphs as
little content streams that each set their own colour.

**A browser rounds its page up.** Asked for 148 × 210 mm Chrome returns
148,17 × 210,23, and it lays the artwork out from the top-left. So each layer
is placed at the sheet's top-left corner and the surplus falls off the right
and the bottom, rather than the artwork being scaled to fit.

## Checking it

```
python render-tests/spot_layers_test.py
```

Runs the real fixtures through and separates the result with Ghostscript: the
page size, one group per finish, the artwork on the spot plate, nothing of it
on a process plate, and each refusal by name.

## Where it runs

There are two implementations, held to the same checks and producing the same
file byte for byte's worth of ink:

| | `container_src/spot_layers.py` | [spot-layers-worker](https://github.com/dennisvandalen/spot-layers-worker) |
| --- | --- | --- |
| Runs on | the container, behind the Hono worker | a Python Worker, on its own |
| PDF library | pikepdf (QPDF) | pypdf |
| The print | decoded by Pillow, re-deflated | its own IDAT bytes, as `/Predictor 15` |
| Needs | Ghostscript, ImageMagick, a cold start | nothing |

The Worker lives in its own repository because this is the one route in this
API that shells out to nothing — it only reads PDFs, rewrites their paints and
writes one back. The rest of the container (the `tiffsep` separations, the
previews, `cairosvg`, the Ghostscript pinned to 9.55) cannot follow it and is
not meant to.

Compared against this implementation on the real foil proofs: the A3 poster
and the A5 sheet come back pixel-identical at 100, 150 and 300 dpi, and
Ghostscript separates them to the same ink on every plate.
