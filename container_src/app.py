from fastapi import FastAPI, HTTPException
from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from pathlib import Path
import subprocess
import re
import pikepdf
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
import pypdfium2 as pdfium
from PIL import Image
import numpy as np
import xml.etree.ElementTree as ET
import urllib.request
from urllib.parse import urlparse
import io
import uuid
import zlib
import base64
import os

app = FastAPI()
@app.get("/")
def root():
    return {"status": "ok", "service": "pdf-api"}

@app.get("/health")
def health():
    result = {"status": "ok", "checks": {}}
    # Check ImageMagick (convert)
    try:
        proc = subprocess.run(["convert", "-version"], capture_output=True, text=True)
        result["checks"]["imagemagick"] = {
            "installed": proc.returncode == 0,
            "version": (proc.stdout or proc.stderr or "").splitlines()[0] if (proc.stdout or proc.stderr) else ""
        }
    except FileNotFoundError:
        result["checks"]["imagemagick"] = {"installed": False}
        result["status"] = "degraded"
    except Exception as e:
        result["checks"]["imagemagick"] = {"installed": False, "error": str(e)}
        result["status"] = "degraded"

    # Check ImageMagick policy (common restrictive setting for PDF)
    policy_summary = {"paths": [], "list_policy": {}}
    policy_paths = ["/etc/ImageMagick-6/policy.xml", "/etc/ImageMagick-7/policy.xml"]
    try:
        for p in policy_paths:
            entry = {"path": p}
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    restrictive = ('rights="none" pattern="PDF"' in content) or (
                        'policy domain="coder" rights="none" pattern="PDF"' in content
                    )
                    entry.update({"present": True, "restrictive": restrictive})
                except Exception as e:
                    entry.update({"present": True, "error": str(e)})
            else:
                entry.update({"present": False})
            policy_summary["paths"].append(entry)

        # Also inspect `convert -list policy`
        lp = subprocess.run(["convert", "-list", "policy"], capture_output=True, text=True)
        output = (lp.stdout or lp.stderr or "")
        restrictive_list = ("PDF" in output) and ("rights=none" in output.lower() or "rights=\"none\"" in output)
        policy_summary["list_policy"] = {
            "ok": lp.returncode == 0,
            "restrictive": restrictive_list,
        }
    except Exception as e:
        policy_summary["error"] = str(e)
        result["status"] = "degraded"
    result["checks"]["imagemagick_policy"] = policy_summary
    return result


class Placement(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    left: Optional[float] = None
    top: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    units: str = Field(default="px", pattern="^(px|pt)$")
    origin: str = Field(default="top-left", pattern="^(top-left|bottom-left)$")


class GenerateRequest(BaseModel):
    base_image: str
    svg_overlay: str
    spot_name: str = "gold"
    allowed_classes: Optional[List[str]] = None
    placement: Optional[Placement] = None


TMP_DIR = Path("/tmp/pdftmp")
TMP_DIR.mkdir(exist_ok=True)
PT_PER_MM = 72.0 / 25.4


def _identify_image_size(image_path: str) -> tuple[int, int]:
    result = subprocess.run([
        "identify", "-format", "%w %h", image_path
    ], capture_output=True, text=True, check=True)
    parts = result.stdout.strip().split()
    return int(parts[0]), int(parts[1])


def _filter_svg_by_classes(source_svg: str, output_svg: Path, allowed_classes: set[str]) -> None:
    tree = ET.parse(source_svg)
    root = tree.getroot()

    def has_allowed_class(elem: ET.Element) -> bool:
        class_attr = elem.attrib.get("class", "")
        if class_attr:
            tokens = {t for t in class_attr.split() if t}
            if tokens & allowed_classes:
                return True
        local_tag = elem.tag.split('}')[-1]
        if local_tag in {"defs", "style"}:
            return True
        return any(has_allowed_class(child) for child in list(elem))

    def prune(elem: ET.Element) -> bool:
        keep = has_allowed_class(elem)
        if not list(elem):
            return keep
        for child in list(elem):
            if not prune(child):
                elem.remove(child)
        return keep or (len(list(elem)) > 0) or elem.tag.split('}')[-1] in {"svg", "defs", "style"}

    prune(root)
    output_svg.write_bytes(ET.tostring(root, encoding="utf-8"))


def _resolve_input_path(path_or_url: str, fallback_ext: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        parsed = urlparse(path_or_url)
        ext = Path(parsed.path).suffix or fallback_ext
        dest = TMP_DIR / f"input-{uuid.uuid4().hex}{ext}"
        req = urllib.request.Request(path_or_url, headers={"User-Agent": "PDF-API/1.0"})
        with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        return str(dest)
    # Map /data/... to container path /app/data/...
    if path_or_url.startswith("/data/"):
        return str(Path("/app") / path_or_url.lstrip("/"))
    return path_or_url


async def _save_upload_tmp(upload: UploadFile, default_ext: str) -> Path:
    filename = upload.filename or ""
    ext = Path(filename).suffix or default_ext
    dest = TMP_DIR / f"upload-{uuid.uuid4().hex}{ext}"
    content = await upload.read()
    with open(dest, "wb") as f:
        f.write(content)
    return dest


def _apply_spot_color_and_overprint(temp_pdf: Path, spot_name: str, output_pdf: Path) -> None:
    with pikepdf.open(temp_pdf) as pdf:
        page = pdf.pages[0]

        if "/Resources" not in page:
            page["/Resources"] = pikepdf.Dictionary()
        resources = page["/Resources"]

        if "/ColorSpace" not in resources:
            resources["/ColorSpace"] = pikepdf.Dictionary()

        tint_transform = pikepdf.Dictionary(
            FunctionType=2,
            Domain=[0, 1],
            C0=[0, 0, 0, 0],
            C1=[0, 0.9, 0, 0],
            N=1,
        )
        resources["/ColorSpace"][f"/{spot_name}"] = pikepdf.Array([
            pikepdf.Name("/Separation"),
            pikepdf.Name(f"/{spot_name}"),
            pikepdf.Name("/DeviceCMYK"),
            tint_transform,
        ])

        if "/ExtGState" not in resources:
            resources["/ExtGState"] = pikepdf.Dictionary()
        gs_dict = pikepdf.Dictionary(
            Type=pikepdf.Name("/ExtGState"),
            OP=True,
            op=True,
            OPM=1,
        )
        resources["/ExtGState"]["/GS1"] = gs_dict

        contents_obj = page["/Contents"]
        if isinstance(contents_obj, pikepdf.Array):
            original_bytes = b"\n".join([s.read_bytes() for s in contents_obj])
        else:
            original_bytes = contents_obj.read_bytes()

        modified = original_bytes
        modified = re.sub(rb"[-+0-9\.\s]+rg\b", b"/" + spot_name.encode("utf-8") + b" cs 1 scn", modified)
        modified = re.sub(rb"[-+0-9\.\s]+g\b", b"/" + spot_name.encode("utf-8") + b" cs 1 scn", modified)
        modified = re.sub(rb"[-+0-9\.\s]+k\b", b"/" + spot_name.encode("utf-8") + b" cs 1 scn", modified)
        modified = re.sub(rb"[-+0-9\.\s]+sc\b", b"1 scn", modified)
        modified = re.sub(rb"[-+0-9\.\s]+RG\b", b"/" + spot_name.encode("utf-8") + b" CS 1 SCN", modified)
        modified = re.sub(rb"[-+0-9\.\s]+G\b", b"/" + spot_name.encode("utf-8") + b" CS 1 SCN", modified)
        modified = re.sub(rb"[-+0-9\.\s]+K\b", b"/" + spot_name.encode("utf-8") + b" CS 1 SCN", modified)
        modified = re.sub(rb"[-+0-9\.\s]+SC\b", b"1 SCN", modified)

        prolog = (
            b"/GS1 gs "
            + b"/" + spot_name.encode("utf-8") + b" cs 1 scn "
            + b"/" + spot_name.encode("utf-8") + b" CS 1 SCN\n"
        )
        new_stream = pikepdf.Stream(pdf, prolog + modified)
        page["/Contents"] = new_stream

        pdf.save(output_pdf)


def _merge_on_top(base_pdf: Path, overlay_pdf: Path, output_pdf: Path, base_image_path: str, placement: Optional[Placement]):
    base_reader = PdfReader(str(base_pdf))
    overlay_reader = PdfReader(str(overlay_pdf))

    base_page = base_reader.pages[0]
    overlay_page = overlay_reader.pages[0]

    base_w_pt = float(base_page.mediabox.width)
    base_h_pt = float(base_page.mediabox.height)
    ovl_w_pt = float(overlay_page.mediabox.width)
    ovl_h_pt = float(overlay_page.mediabox.height)

    if placement:
        units = placement.units
        origin = placement.origin
        x_raw = placement.x if placement.x is not None else placement.left
        y_raw = placement.y
        top_raw = placement.top
        width_raw = placement.width
        height_raw = placement.height

        if units == "px":
            base_w_px, base_h_px = _identify_image_size(base_image_path)
            px_to_pt_x = base_w_pt / base_w_px if base_w_px else (72.0 / 300.0)
            px_to_pt_y = base_h_pt / base_h_px if base_h_px else (72.0 / 300.0)

            x_pt = float(x_raw) * px_to_pt_x if x_raw is not None else 0.0
            w_pt = float(width_raw) * px_to_pt_x if width_raw is not None else None
            h_pt = float(height_raw) * px_to_pt_y if height_raw is not None else None

            if w_pt is None and h_pt is None:
                w_pt, h_pt = ovl_w_pt, ovl_h_pt
            elif w_pt is None:
                w_pt = ovl_w_pt * (h_pt / ovl_h_pt if ovl_h_pt else 1.0)
            elif h_pt is None:
                h_pt = ovl_h_pt * (w_pt / ovl_w_pt if ovl_w_pt else 1.0)

            if top_raw is not None:
                top_pt = float(top_raw) * px_to_pt_y
                y_pt = base_h_pt - top_pt - h_pt
            else:
                y_val = float(y_raw) if y_raw is not None else 0.0
                y_pt = y_val * px_to_pt_y
                if origin == "top-left":
                    y_pt = base_h_pt - y_pt - h_pt
        else:
            x_pt = float(x_raw) if x_raw is not None else 0.0
            w_pt = float(width_raw) if width_raw is not None else None
            h_pt = float(height_raw) if height_raw is not None else None

            if w_pt is None and h_pt is None:
                w_pt, h_pt = ovl_w_pt, ovl_h_pt
            elif w_pt is None:
                w_pt = ovl_w_pt * (h_pt / ovl_h_pt if ovl_h_pt else 1.0)
            elif h_pt is None:
                h_pt = ovl_h_pt * (w_pt / ovl_w_pt if ovl_w_pt else 1.0)

            if top_raw is not None:
                y_pt = base_h_pt - float(top_raw) - h_pt
            else:
                y_pt = float(y_raw) if y_raw is not None else 0.0
                if origin == "top-left":
                    y_pt = base_h_pt - y_pt - h_pt

        sx = (w_pt / ovl_w_pt) if ovl_w_pt else 1.0
        sy = (h_pt / ovl_h_pt) if ovl_h_pt else 1.0
        transform = Transformation().scale(sx, sy).translate(x_pt, y_pt)
        base_page.merge_transformed_page(overlay_page, transform)
    else:
        sx = base_w_pt / ovl_w_pt if ovl_w_pt else 1.0
        sy = base_h_pt / ovl_h_pt if ovl_h_pt else 1.0
        transform = Transformation().scale(sx, sy)
        base_page.merge_transformed_page(overlay_page, transform)

    writer = PdfWriter()
    writer.add_page(base_page)
    with open(output_pdf, "wb") as f_out:
        writer.write(f_out)


@app.post("/generate")
async def generate(req: GenerateRequest):
    try:
        # Support built-in test assets via special tokens
        if req.base_image == "@sample":
            base_image = str(Path("/app/assets/130960.png"))
        else:
            base_image = _resolve_input_path(req.base_image, ".png")

        if req.svg_overlay == "@sample":
            svg_overlay = str(Path("/app/assets/star_overlay.svg"))
        else:
            svg_overlay = _resolve_input_path(req.svg_overlay, ".svg")
        spot_name = req.spot_name
        allowed_classes = req.allowed_classes
        placement = req.placement

        BASE_PDF = TMP_DIR / "base.pdf"
        OVERLAY_TEMP_PDF = TMP_DIR / "overlay-temp.pdf"
        OVERLAY_SPOT_PDF = TMP_DIR / "overlay-spot.pdf"
        FILTERED_SVG = TMP_DIR / "overlay-filtered.svg"
        OUTPUT_PDF = TMP_DIR / "final.pdf"

        try:
            subprocess.run(
                [
                    "convert",
                    "+profile",
                    "icc",
                    "-density",
                    "300",
                    base_image,
                    "-colorspace",
                    "CMYK",
                    str(BASE_PDF),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            return JSONResponse(status_code=500, content={
                "step": "imagemagick-convert",
                "error": f"convert failed: {e}",
                "stderr": e.stderr,
                "stdout": e.stdout,
            })

        svg_to_convert = svg_overlay
        if allowed_classes:
            _filter_svg_by_classes(svg_overlay, FILTERED_SVG, set(allowed_classes))
            svg_to_convert = str(FILTERED_SVG)

        try:
            subprocess.run(
                ["cairosvg", svg_to_convert, "-o", str(OVERLAY_TEMP_PDF), "-f", "pdf"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            return JSONResponse(status_code=500, content={
                "step": "cairosvg",
                "error": f"cairosvg failed: {e}",
                "stderr": e.stderr,
                "stdout": e.stdout,
            })

        _apply_spot_color_and_overprint(OVERLAY_TEMP_PDF, spot_name, OVERLAY_SPOT_PDF)
        _merge_on_top(BASE_PDF, OVERLAY_SPOT_PDF, OUTPUT_PDF, base_image, placement)

        return FileResponse(path=str(OUTPUT_PDF), media_type="application/pdf", filename="final.pdf")
    except subprocess.CalledProcessError as e:
        return JSONResponse(status_code=500, content={"error": f"Subprocess failed: {e}"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FlattenRequest(BaseModel):
    input_pdf: str
    dpi: int = 300


@app.post("/flatten")
async def flatten(req: FlattenRequest):
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        output_path = TMP_DIR / f"flattened-{uuid.uuid4().hex}.pdf"
        try:
            # Render each page with PDFium, composite onto white, and write a new PDF of images
            doc = pdfium.PdfDocument(str(input_path))
            rendered_images = []
            scale = max(36, min(1200, req.dpi)) / 72.0
            for idx in range(len(doc)):
                page = doc[idx]
                pil_img = page.render_topil(scale=scale) if hasattr(page, "render_topil") else None
                if pil_img is None:
                    bitmap = page.render(scale=scale)
                    if hasattr(bitmap, "to_pil"):
                        pil_img = bitmap.to_pil()
                    else:
                        bmp_bytes = bitmap.to_bytes()
                        img_w, img_h = bitmap.get_size()
                        pil_img = Image.frombytes("RGBA", (img_w, img_h), bmp_bytes, "raw", "BGRA")
                # Ensure no alpha (flatten onto white)
                if pil_img.mode == "RGBA":
                    bg = Image.new("RGB", pil_img.size, (255, 255, 255))
                    bg.paste(pil_img, mask=pil_img.split()[3])
                    pil_img = bg
                else:
                    pil_img = pil_img.convert("RGB")
                rendered_images.append(pil_img)

            if not rendered_images:
                return JSONResponse(status_code=400, content={"error": "PDF has no pages"})

            first, rest = rendered_images[0], rendered_images[1:]
            first.save(
                str(output_path),
                format="PDF",
                save_all=True,
                append_images=rest,
                resolution=max(36, min(1200, req.dpi)),
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "step": "pdfium-flatten",
                "error": str(e),
            })
        return FileResponse(path=str(output_path), media_type="application/pdf", filename="flattened.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/flatten/upload")
async def flatten_upload(file: UploadFile = File(...), dpi: int = Form(300)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await flatten(FlattenRequest(input_pdf=str(tmp_path), dpi=dpi))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class PageDimensionsRequest(BaseModel):
    input_pdf: str


@app.post("/page-dimensions")
async def page_dimensions(req: PageDimensionsRequest):
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        reader = PdfReader(input_path)
        pages = []
        for i, page in enumerate(reader.pages):
            w_pt = float(page.mediabox.width)
            h_pt = float(page.mediabox.height)
            pages.append({
                "index": i,
                "width_mm": round(w_pt / PT_PER_MM, 3),
                "height_mm": round(h_pt / PT_PER_MM, 3),
                "width_pt": round(w_pt, 3),
                "height_pt": round(h_pt, 3),
            })
        return {"page_count": len(pages), "pages": pages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/page-dimensions/upload")
async def page_dimensions_upload(file: UploadFile = File(...)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await page_dimensions(PageDimensionsRequest(input_pdf=str(tmp_path)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CropFitRequest(BaseModel):
    input_pdf: str
    width_mm: float
    height_mm: float
    anchor: str = Field(default="center", pattern="^(center)$")


@app.post("/crop-fit-mm")
async def crop_fit_mm(req: CropFitRequest):
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        reader = PdfReader(input_path)
        writer = PdfWriter()
        target_w_pt = req.width_mm * PT_PER_MM
        target_h_pt = req.height_mm * PT_PER_MM
        for page in reader.pages:
            w_pt = float(page.mediabox.width)
            h_pt = float(page.mediabox.height)
            if w_pt >= target_w_pt and h_pt >= target_h_pt:
                x0 = (w_pt - target_w_pt) / 2.0
                y0 = (h_pt - target_h_pt) / 2.0
                x1 = x0 + target_w_pt
                y1 = y0 + target_h_pt
                page.mediabox = RectangleObject([x0, y0, x1, y1])
                page.cropbox = RectangleObject([x0, y0, x1, y1])
            # If the page is smaller in any dimension, leave it unchanged
            writer.add_page(page)
        output_path = TMP_DIR / f"cropped-{uuid.uuid4().hex}.pdf"
        with open(output_path, "wb") as f_out:
            writer.write(f_out)
        return FileResponse(path=str(output_path), media_type="application/pdf", filename="cropped.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/crop-fit-mm/upload")
async def crop_fit_mm_upload(
    file: UploadFile = File(...),
    width_mm: float = Form(...),
    height_mm: float = Form(...),
):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await crop_fit_mm(CropFitRequest(input_pdf=str(tmp_path), width_mm=width_mm, height_mm=height_mm))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class OverprintPreviewRequest(BaseModel):
    input_pdf: str
    page: int = 0
    dpi: int = 150


@app.post("/overprint-preview")
async def overprint_preview(req: OverprintPreviewRequest):
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        page_index_1based = max(1, req.page + 1)
        
        # Calculate optimal DPI for 500x500 output to avoid over-rendering
        # Get page dimensions first
        try:
            with pikepdf.open(input_path) as pdf:
                if len(pdf.pages) >= page_index_1based:
                    page = pdf.pages[page_index_1based - 1]
                    mediabox = page.mediabox
                    page_width_pt = float(mediabox[2] - mediabox[0])
                    page_height_pt = float(mediabox[3] - mediabox[1])
                    
                    # Calculate DPI needed for 500px on longest edge
                    max_dimension_pt = max(page_width_pt, page_height_pt)
                    optimal_dpi = int((500 / max_dimension_pt) * 72)
                    # Clamp to 72 DPI max for speed (good enough for preview)
                    dpi = min(72, optimal_dpi)
                else:
                    dpi = 72
        except Exception:
            dpi = 72
        
        # Detect spot colors using pikepdf
        spot_colors = []
        spot_tint_colors = {}
        try:
            with pikepdf.open(input_path) as pdf:
                if len(pdf.pages) >= page_index_1based:
                    page = pdf.pages[page_index_1based - 1]
                    if "/Resources" in page and "/ColorSpace" in page["/Resources"]:
                        colorspaces = page["/Resources"]["/ColorSpace"]
                        for name, cs in colorspaces.items():
                            if isinstance(cs, pikepdf.Array) and len(cs) > 0:
                                if str(cs[0]) == "/Separation":
                                    spot_name = str(cs[1]).lstrip("/")
                                    spot_colors.append(spot_name)
                                    if len(cs) > 3 and isinstance(cs[3], pikepdf.Dictionary):
                                        tint_fn = cs[3]
                                        if "/C1" in tint_fn:
                                            c1 = tint_fn["/C1"]
                                            if isinstance(c1, pikepdf.Array) and len(c1) >= 4:
                                                cmyk = [float(c1[i]) for i in range(4)]
                                                r = int(255 * (1 - cmyk[0]) * (1 - cmyk[3]))
                                                g = int(255 * (1 - cmyk[1]) * (1 - cmyk[3]))
                                                b = int(255 * (1 - cmyk[2]) * (1 - cmyk[3]))
                                                spot_tint_colors[spot_name] = (r, g, b)
        except Exception:
            pass
        
        # If no spot colors, render simple composite and return
        if not spot_colors:
            composite_path = TMP_DIR / f"composite-{uuid.uuid4().hex}.png"
            try:
                subprocess.run([
                    "gs",
                    "-dSAFER",
                    "-dNOPAUSE",
                    "-dQUIET",
                    "-dBATCH",
                    "-sDEVICE=png16m",
                    f"-r{dpi}",
                    f"-dFirstPage={page_index_1based}",
                    f"-dLastPage={page_index_1based}",
                    f"-sOutputFile={composite_path}",
                    input_path,
                ], check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                return JSONResponse(status_code=500, content={
                    "step": "ghostscript-composite-render",
                    "error": f"ghostscript failed: {e}",
                    "stderr": e.stderr,
                    "stdout": e.stdout,
                })
            
            composite_img = Image.open(composite_path)
            composite_img.thumbnail((500, 500), Image.Resampling.BILINEAR)
            img_path = TMP_DIR / f"op-preview-{uuid.uuid4().hex}.png"
            composite_img.save(img_path, format="PNG")
            return FileResponse(path=str(img_path), media_type="image/png", filename="overprint-preview.png")
        
        # Extract spot separations using tiffsep at low DPI for speed
        sep_dir = TMP_DIR / f"sep-{uuid.uuid4().hex}"
        sep_dir.mkdir(exist_ok=True)
        sep_prefix = sep_dir / "page"
        
        try:
            subprocess.run([
                "gs",
                "-dSAFER",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                "-sDEVICE=tiffsep",
                f"-r{dpi}",
                f"-dFirstPage={page_index_1based}",
                f"-dLastPage={page_index_1based}",
                f"-sOutputFile={sep_prefix}.tif",
                input_path,
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return JSONResponse(status_code=500, content={
                "step": "ghostscript-tiffsep",
                "error": f"ghostscript failed: {e}",
                "stderr": e.stderr,
                "stdout": e.stdout,
            })
        
        # Reconstruct CMYK base from separations (without spots)
        sep_files = sorted(sep_dir.glob("*.tif"))
        cmyk_seps = {"Cyan": None, "Magenta": None, "Yellow": None, "Black": None}
        spot_seps = {}
        
        for sep_file in sep_files:
            sep_name = sep_file.stem.replace("page.", "").replace("page(", "").replace(")", "")
            sep_img = Image.open(sep_file).convert("L")
            
            if sep_name in cmyk_seps:
                cmyk_seps[sep_name] = sep_img
            elif sep_name in spot_colors:
                spot_seps[sep_name] = sep_img
        
        # Reconstruct RGB from CMYK separations only
        if all(cmyk_seps.values()):
            # Convert separations to numpy arrays for fast processing
            c_arr = np.array(cmyk_seps["Cyan"], dtype=np.float32) / 255.0
            m_arr = np.array(cmyk_seps["Magenta"], dtype=np.float32) / 255.0
            y_arr = np.array(cmyk_seps["Yellow"], dtype=np.float32) / 255.0
            k_arr = np.array(cmyk_seps["Black"], dtype=np.float32) / 255.0
            
            # CMYK to RGB conversion (subtractive color model)
            # In tiffsep, black pixels = ink, white = no ink, so we need to invert
            c_arr = 1.0 - c_arr
            m_arr = 1.0 - m_arr
            y_arr = 1.0 - y_arr
            k_arr = 1.0 - k_arr
            
            # Standard CMYK to RGB formula
            r = 255 * (1.0 - c_arr) * (1.0 - k_arr)
            g = 255 * (1.0 - m_arr) * (1.0 - k_arr)
            b = 255 * (1.0 - y_arr) * (1.0 - k_arr)
            
            # Stack into RGB image
            rgb_arr = np.stack([r, g, b], axis=2).astype(np.uint8)
            result_img = Image.fromarray(rgb_arr, mode="RGB").convert("RGBA")
        else:
            # Fallback: render composite if CMYK reconstruction fails
            composite_path = TMP_DIR / f"composite-{uuid.uuid4().hex}.png"
            subprocess.run([
                "gs",
                "-dSAFER",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                "-sDEVICE=png16m",
                f"-r{dpi}",
                f"-dFirstPage={page_index_1based}",
                f"-dLastPage={page_index_1based}",
                f"-sOutputFile={composite_path}",
                input_path,
            ], check=True, capture_output=True, text=True)
            result_img = Image.open(composite_path).convert("RGBA")
        
        # Now overlay spot colors with transparency
        for spot_name, sep_img in spot_seps.items():
            tint_color = spot_tint_colors.get(spot_name, (255, 0, 255))
            
            # Create colored overlay
            spot_layer = Image.new("RGBA", sep_img.size, tint_color + (0,))
            # Use separation as alpha (inverted and at 50% opacity)
            spot_alpha = sep_img.point(lambda x: int((255 - x) * 0.5))
            spot_layer.putalpha(spot_alpha)
            
            # Composite over result
            result_img = Image.alpha_composite(result_img, spot_layer)
        
        # Resize to max 500x500 while maintaining aspect ratio (use BILINEAR for speed)
        result_img.thumbnail((500, 500), Image.Resampling.BILINEAR)
        
        img_path = TMP_DIR / f"op-preview-{uuid.uuid4().hex}.png"
        result_img.save(img_path, format="PNG")
        return FileResponse(path=str(img_path), media_type="image/png", filename="overprint-preview.png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/overprint-preview/upload")
async def overprint_preview_upload(file: UploadFile = File(...), page: int = Form(0), dpi: int = Form(150)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await overprint_preview(OverprintPreviewRequest(input_pdf=str(tmp_path), page=page, dpi=dpi))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RenderLayerRequest(BaseModel):
    input_pdf: str
    page: int = 0
    dpi: int = 150
    max_size: int = 500


@app.post("/render-cmyk-only")
async def render_cmyk_only(req: RenderLayerRequest):
    """Render PDF with only CMYK process colors, ignoring spot colors"""
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        page_index_1based = max(1, req.page + 1)
        
        # Calculate optimal DPI for output size
        try:
            with pikepdf.open(input_path) as pdf:
                if len(pdf.pages) >= page_index_1based:
                    page = pdf.pages[page_index_1based - 1]
                    mediabox = page.mediabox
                    page_width_pt = float(mediabox[2] - mediabox[0])
                    page_height_pt = float(mediabox[3] - mediabox[1])
                    max_dimension_pt = max(page_width_pt, page_height_pt)
                    optimal_dpi = int((req.max_size / max_dimension_pt) * 72)
                    dpi = min(72, optimal_dpi)
                else:
                    dpi = 72
        except Exception:
            dpi = 72
        
        # Use tiffsep to extract separations, then reconstruct from CMYK only
        sep_dir = TMP_DIR / f"sep-{uuid.uuid4().hex}"
        sep_dir.mkdir(exist_ok=True)
        sep_prefix = sep_dir / "page"
        
        try:
            subprocess.run([
                "gs",
                "-dSAFER",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                "-sDEVICE=tiffsep",
                f"-r{dpi}",
                f"-dFirstPage={page_index_1based}",
                f"-dLastPage={page_index_1based}",
                f"-sOutputFile={sep_prefix}.tif",
                input_path,
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return JSONResponse(status_code=500, content={
                "step": "ghostscript-tiffsep",
                "error": f"ghostscript failed: {e}",
                "stderr": e.stderr,
                "stdout": e.stdout,
            })
        
        # Load only CMYK separations
        sep_files = sorted(sep_dir.glob("*.tif"))
        cmyk_seps = {"Cyan": None, "Magenta": None, "Yellow": None, "Black": None}
        
        for sep_file in sep_files:
            sep_name = sep_file.stem.replace("page.", "").replace("page(", "").replace(")", "")
            if sep_name in cmyk_seps:
                cmyk_seps[sep_name] = Image.open(sep_file).convert("L")
        
        # Reconstruct RGB from CMYK only
        if all(cmyk_seps.values()):
            c_arr = np.array(cmyk_seps["Cyan"], dtype=np.float32) / 255.0
            m_arr = np.array(cmyk_seps["Magenta"], dtype=np.float32) / 255.0
            y_arr = np.array(cmyk_seps["Yellow"], dtype=np.float32) / 255.0
            k_arr = np.array(cmyk_seps["Black"], dtype=np.float32) / 255.0
            
            # Invert (tiffsep: black=ink, white=no ink)
            c_arr = 1.0 - c_arr
            m_arr = 1.0 - m_arr
            y_arr = 1.0 - y_arr
            k_arr = 1.0 - k_arr
            
            # CMYK to RGB
            r = 255 * (1.0 - c_arr) * (1.0 - k_arr)
            g = 255 * (1.0 - m_arr) * (1.0 - k_arr)
            b = 255 * (1.0 - y_arr) * (1.0 - k_arr)
            
            rgb_arr = np.stack([r, g, b], axis=2).astype(np.uint8)
            result_img = Image.fromarray(rgb_arr, mode="RGB")
        else:
            return JSONResponse(status_code=500, content={"error": "Could not extract CMYK separations"})
        
        # Resize to max size
        result_img.thumbnail((req.max_size, req.max_size), Image.Resampling.BILINEAR)
        
        img_path = TMP_DIR / f"cmyk-only-{uuid.uuid4().hex}.png"
        result_img.save(img_path, format="PNG")
        return FileResponse(path=str(img_path), media_type="image/png", filename="cmyk-only.png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/render-cmyk-only/upload")
async def render_cmyk_only_upload(file: UploadFile = File(...), page: int = Form(0), dpi: int = Form(150), max_size: int = Form(500)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await render_cmyk_only(RenderLayerRequest(input_pdf=str(tmp_path), page=page, dpi=dpi, max_size=max_size))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/render-spots-only")
async def render_spots_only(req: RenderLayerRequest):
    """Render only spot color layers as semi-transparent overlay"""
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        page_index_1based = max(1, req.page + 1)
        
        # Calculate optimal DPI
        try:
            with pikepdf.open(input_path) as pdf:
                if len(pdf.pages) >= page_index_1based:
                    page = pdf.pages[page_index_1based - 1]
                    mediabox = page.mediabox
                    page_width_pt = float(mediabox[2] - mediabox[0])
                    page_height_pt = float(mediabox[3] - mediabox[1])
                    max_dimension_pt = max(page_width_pt, page_height_pt)
                    optimal_dpi = int((req.max_size / max_dimension_pt) * 72)
                    dpi = min(72, optimal_dpi)
                else:
                    dpi = 72
        except Exception:
            dpi = 72
        
        # Detect spot colors
        spot_colors = []
        spot_tint_colors = {}
        try:
            with pikepdf.open(input_path) as pdf:
                if len(pdf.pages) >= page_index_1based:
                    page = pdf.pages[page_index_1based - 1]
                    if "/Resources" in page and "/ColorSpace" in page["/Resources"]:
                        colorspaces = page["/Resources"]["/ColorSpace"]
                        for name, cs in colorspaces.items():
                            if isinstance(cs, pikepdf.Array) and len(cs) > 0:
                                if str(cs[0]) == "/Separation":
                                    spot_name = str(cs[1]).lstrip("/")
                                    spot_colors.append(spot_name)
                                    if len(cs) > 3 and isinstance(cs[3], pikepdf.Dictionary):
                                        tint_fn = cs[3]
                                        if "/C1" in tint_fn:
                                            c1 = tint_fn["/C1"]
                                            if isinstance(c1, pikepdf.Array) and len(c1) >= 4:
                                                cmyk = [float(c1[i]) for i in range(4)]
                                                r = int(255 * (1 - cmyk[0]) * (1 - cmyk[3]))
                                                g = int(255 * (1 - cmyk[1]) * (1 - cmyk[3]))
                                                b = int(255 * (1 - cmyk[2]) * (1 - cmyk[3]))
                                                spot_tint_colors[spot_name] = (r, g, b)
        except Exception:
            pass
        
        if not spot_colors:
            return JSONResponse(status_code=404, content={"error": "No spot colors found in PDF"})
        
        # Extract separations
        sep_dir = TMP_DIR / f"sep-{uuid.uuid4().hex}"
        sep_dir.mkdir(exist_ok=True)
        sep_prefix = sep_dir / "page"
        
        try:
            subprocess.run([
                "gs",
                "-dSAFER",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                "-sDEVICE=tiffsep",
                f"-r{dpi}",
                f"-dFirstPage={page_index_1based}",
                f"-dLastPage={page_index_1based}",
                f"-sOutputFile={sep_prefix}.tif",
                input_path,
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return JSONResponse(status_code=500, content={
                "step": "ghostscript-tiffsep",
                "error": f"ghostscript failed: {e}",
                "stderr": e.stderr,
                "stdout": e.stdout,
            })
        
        # Load only spot separations
        sep_files = sorted(sep_dir.glob("*.tif"))
        spot_seps = {}
        first_sep = None
        
        for sep_file in sep_files:
            sep_name = sep_file.stem.replace("page.", "").replace("page(", "").replace(")", "")
            sep_img = Image.open(sep_file).convert("L")
            if first_sep is None:
                first_sep = sep_img
            if sep_name in spot_colors:
                spot_seps[sep_name] = sep_img
        
        if not spot_seps or first_sep is None:
            return JSONResponse(status_code=404, content={"error": "No spot separations found"})
        
        # Create transparent image with spot overlays
        result_img = Image.new("RGBA", first_sep.size, (0, 0, 0, 0))
        
        for spot_name, sep_img in spot_seps.items():
            tint_color = spot_tint_colors.get(spot_name, (255, 0, 255))
            
            # Create colored overlay
            spot_layer = Image.new("RGBA", sep_img.size, tint_color + (0,))
            # Use separation as alpha (inverted, full opacity)
            spot_alpha = sep_img.point(lambda x: 255 - x)
            spot_layer.putalpha(spot_alpha)
            
            # Composite over result
            result_img = Image.alpha_composite(result_img, spot_layer)
        
        # Resize to max size
        result_img.thumbnail((req.max_size, req.max_size), Image.Resampling.BILINEAR)
        
        img_path = TMP_DIR / f"spots-only-{uuid.uuid4().hex}.png"
        result_img.save(img_path, format="PNG")
        return FileResponse(path=str(img_path), media_type="image/png", filename="spots-only.png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/render-spots-only/upload")
async def render_spots_only_upload(file: UploadFile = File(...), page: int = Form(0), dpi: int = Form(150), max_size: int = Form(500)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await render_spots_only(RenderLayerRequest(input_pdf=str(tmp_path), page=page, dpi=dpi, max_size=max_size))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class QuickPreviewRequest(BaseModel):
    input_pdf: str
    page: int = 0
    max_size: int = 500


@app.post("/quick-preview")
async def quick_preview(req: QuickPreviewRequest):
    """Fast preview - removes spot color content from PDF then renders with PDFium"""
    try:
        input_path = _resolve_input_path(req.input_pdf, ".pdf")
        page_index_0based = max(0, req.page)
        
        # Remove spot color content from PDF
        no_spots_pdf = TMP_DIR / f"no-spots-{uuid.uuid4().hex}.pdf"
        try:
            with pikepdf.open(input_path) as pdf:
                for page in pdf.pages:
                    if "/Resources" not in page or "/ColorSpace" not in page["/Resources"]:
                        continue
                    
                    colorspaces = page["/Resources"]["/ColorSpace"]
                    spot_names = []
                    
                    # Find all spot color names
                    for name, cs in colorspaces.items():
                        if isinstance(cs, pikepdf.Array) and len(cs) > 0:
                            if str(cs[0]) == "/Separation":
                                spot_names.append(str(name).lstrip("/"))
                    
                    if not spot_names:
                        continue
                    
                    # Get content stream
                    contents_obj = page["/Contents"]
                    if isinstance(contents_obj, pikepdf.Array):
                        original_bytes = b"\n".join([s.read_bytes() for s in contents_obj])
                    else:
                        original_bytes = contents_obj.read_bytes()
                    
                    # Remove content that uses spot colors
                    modified = original_bytes
                    for spot_name in spot_names:
                        spot_bytes = spot_name.encode('utf-8')
                        lines = modified.split(b'\n')
                        filtered_lines = []
                        skip_until_end = False
                        
                        for line in lines:
                            # Skip lines that reference the spot color
                            if spot_bytes in line and (b' cs' in line or b' CS' in line or b' scn' in line or b' SCN' in line):
                                skip_until_end = True
                                continue
                            # Skip drawing operations after spot color is set until color is changed
                            if skip_until_end:
                                # Check if this line changes color space back
                                if b' cs' in line or b' CS' in line or b' rg' in line or b' RG' in line or b' k' in line or b' K' in line:
                                    skip_until_end = False
                                    if spot_bytes not in line:
                                        filtered_lines.append(line)
                                # Skip drawing commands while in spot color
                                continue
                            filtered_lines.append(line)
                        
                        modified = b'\n'.join(filtered_lines)
                    
                    # Update content stream
                    new_stream = pikepdf.Stream(pdf, modified)
                    page["/Contents"] = new_stream
                    
                    # Remove spot color spaces
                    for spot_name in spot_names:
                        spot_key = "/" + spot_name
                        if spot_key in colorspaces:
                            del colorspaces[spot_key]
                
                pdf.save(no_spots_pdf)
        except Exception as e:
            # If spot removal fails, use original
            no_spots_pdf = Path(input_path)
        
        # Use PDFium for faster rendering (much faster than Ghostscript)
        pdf_doc = pdfium.PdfDocument(str(no_spots_pdf))
        if page_index_0based >= len(pdf_doc):
            raise HTTPException(status_code=400, detail=f"Page {page_index_0based} out of range")
        
        page = pdf_doc[page_index_0based]
        
        # Calculate scale to fit within max_size
        width_pt = page.get_width()
        height_pt = page.get_height()
        scale = min(req.max_size / width_pt, req.max_size / height_pt)
        
        # Render directly at target size (no need for high DPI + resize)
        bitmap = page.render(scale=scale, rotation=0)
        pil_image = bitmap.to_pil()
        
        output_path = TMP_DIR / f"preview-{uuid.uuid4().hex}.png"
        pil_image.save(output_path, format="PNG")
        return FileResponse(path=str(output_path), media_type="image/png", filename="preview.png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/quick-preview/upload")
async def quick_preview_upload(file: UploadFile = File(...), page: int = Form(0), max_size: int = Form(500)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        return await quick_preview(QuickPreviewRequest(input_pdf=str(tmp_path), page=page, max_size=max_size))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _create_spot_color_pdf_from_image(
    image_path: str,
    spot_name: str,
    dpi: int,
    include_image: bool = False,
    include_spot: bool = True,
) -> Path:
    """Create a PDF with optional RGB base image and/or a Separation (spot) color layer.

    - include_image: draw the original image as RGB with SMask for transparency
    - include_spot: draw a spot color layer where non-transparent pixels become spot ink
    """
    img = Image.open(image_path).convert("RGBA")
    alpha = img.split()[3]  # 0 = transparent, 255 = opaque
    width_px, height_px = img.size

    # Page size in points at the given DPI
    width_pt = width_px * 72.0 / dpi
    height_pt = height_px * 72.0 / dpi

    # Alpha channel as raw bytes (single component per pixel)
    alpha_bytes = alpha.tobytes()

    output_pdf = TMP_DIR / f"spot-{uuid.uuid4().hex}.pdf"

    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(width_pt, height_pt))
        page = pdf.pages[0]
        page["/Resources"] = pikepdf.Dictionary()

        resources = page["/Resources"]
        resources["/XObject"] = pikepdf.Dictionary()
        resources["/Properties"] = pikepdf.Dictionary()

        # OCG (Optional Content Group) for spot color layer
        spot_ocg = None
        base_ocg = None
        ocgs_list = []
        if include_spot:
            spot_ocg = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/OCG"),
                Name=spot_name,
                Usage=pikepdf.Dictionary(
                    Print=pikepdf.Dictionary(
                        Subtype=pikepdf.Name("/Print"),
                        PrintState=pikepdf.Name("/ON"),
                    ),
                ),
            ))
            resources["/Properties"]["/SpotOC"] = spot_ocg
            ocgs_list.append(spot_ocg)
        if include_image:
            base_ocg = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/OCG"),
                Name="Image",
            ))
            resources["/Properties"]["/BaseOC"] = base_ocg
            ocgs_list.append(base_ocg)

        if ocgs_list:
            pdf.Root["/OCProperties"] = pikepdf.Dictionary(
                OCGs=pikepdf.Array(ocgs_list),
                D=pikepdf.Dictionary(
                    Order=pikepdf.Array(ocgs_list),
                    ON=pikepdf.Array(ocgs_list),
                    OFF=pikepdf.Array([]),
                ),
            )

        content_parts = []

        # Original image as RGB base layer with alpha mask
        if include_image:
            rgb_img = img.convert("RGB")
            rgb_bytes = zlib.compress(rgb_img.tobytes())
            base_stream = pikepdf.Stream(pdf, rgb_bytes)
            base_stream["/Type"] = pikepdf.Name("/XObject")
            base_stream["/Subtype"] = pikepdf.Name("/Image")
            base_stream["/Width"] = width_px
            base_stream["/Height"] = height_px
            base_stream["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
            base_stream["/BitsPerComponent"] = 8
            base_stream["/Filter"] = pikepdf.Name("/FlateDecode")

            # SMask for transparency
            smask_stream = pikepdf.Stream(pdf, zlib.compress(alpha_bytes))
            smask_stream["/Type"] = pikepdf.Name("/XObject")
            smask_stream["/Subtype"] = pikepdf.Name("/Image")
            smask_stream["/Width"] = width_px
            smask_stream["/Height"] = height_px
            smask_stream["/ColorSpace"] = pikepdf.Name("/DeviceGray")
            smask_stream["/BitsPerComponent"] = 8
            smask_stream["/Filter"] = pikepdf.Name("/FlateDecode")
            base_stream["/SMask"] = smask_stream

            resources["/XObject"]["/BaseImg"] = base_stream

            content_parts.append(
                f"/OC /BaseOC BDC\n"
                f"q\n"
                f"{width_pt:.4f} 0 0 {height_pt:.4f} 0 0 cm\n"
                f"/BaseImg Do\n"
                f"Q\n"
                f"EMC\n"
            )

        if include_spot:
            resources["/ColorSpace"] = pikepdf.Dictionary()
            tint_transform = pikepdf.Dictionary(
                FunctionType=2,
                Domain=[0, 1],
                C0=[0, 0, 0, 0],
                C1=[0, 0.9, 0, 0],
                N=1,
            )
            resources["/ColorSpace"]["/SpotCS"] = pikepdf.Array([
                pikepdf.Name("/Separation"),
                pikepdf.Name(f"/{spot_name}"),
                pikepdf.Name("/DeviceCMYK"),
                tint_transform,
            ])

            compressed = zlib.compress(alpha_bytes)
            img_stream = pikepdf.Stream(pdf, compressed)
            img_stream["/Type"] = pikepdf.Name("/XObject")
            img_stream["/Subtype"] = pikepdf.Name("/Image")
            img_stream["/Width"] = width_px
            img_stream["/Height"] = height_px
            img_stream["/ColorSpace"] = pikepdf.Array([
                pikepdf.Name("/Separation"),
                pikepdf.Name(f"/{spot_name}"),
                pikepdf.Name("/DeviceCMYK"),
                tint_transform,
            ])
            img_stream["/BitsPerComponent"] = 8
            img_stream["/Filter"] = pikepdf.Name("/FlateDecode")
            resources["/XObject"]["/SpotImg"] = img_stream

            resources["/ExtGState"] = pikepdf.Dictionary()
            resources["/ExtGState"]["/GS1"] = pikepdf.Dictionary(
                Type=pikepdf.Name("/ExtGState"),
                OP=True,
                op=True,
                OPM=1,
            )

            content_parts.append(
                f"/OC /SpotOC BDC\n"
                f"/GS1 gs\n"
                f"q\n"
                f"{width_pt:.4f} 0 0 {height_pt:.4f} 0 0 cm\n"
                f"/SpotImg Do\n"
                f"Q\n"
                f"EMC\n"
            )

        page["/Contents"] = pikepdf.Stream(pdf, "".join(content_parts).encode())
        pdf.save(output_pdf)

    return output_pdf


@app.post("/spot-color-layer")
async def spot_color_layer(
    file: UploadFile = File(...),
    spot_name: str = Form("white"),
    dpi: int = Form(300),
):
    try:
        tmp_path = await _save_upload_tmp(file, ".png")
        output = _create_spot_color_pdf_from_image(str(tmp_path), spot_name, dpi, include_image=False, include_spot=True)
        return FileResponse(path=str(output), media_type="application/pdf", filename="spot-color-layer.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/image-only")
async def image_only(
    file: UploadFile = File(...),
    dpi: int = Form(300),
):
    try:
        tmp_path = await _save_upload_tmp(file, ".png")
        output = _create_spot_color_pdf_from_image(str(tmp_path), "white", dpi, include_image=True, include_spot=False)
        return FileResponse(path=str(output), media_type="application/pdf", filename="image-only.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/image-with-spot-color")
async def image_with_spot_color(
    file: UploadFile = File(...),
    spot_name: str = Form("white"),
    dpi: int = Form(300),
):
    try:
        tmp_path = await _save_upload_tmp(file, ".png")
        output = _create_spot_color_pdf_from_image(str(tmp_path), spot_name, dpi, include_image=True, include_spot=True)
        return FileResponse(path=str(output), media_type="application/pdf", filename="image-with-spot-color.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/debug")
async def debug_pdf(file: UploadFile = File(...), dpi: int = Form(150)):
    try:
        tmp_path = await _save_upload_tmp(file, ".pdf")
        result = {"spot_colors": [], "pages": [], "images": [], "previews": {}}

        # Extract spot colors via mutool
        grep_result = subprocess.run(
            ["mutool", "show", str(tmp_path), "grep"],
            capture_output=True, text=True
        )
        for line in grep_result.stdout.splitlines():
            if "/Separation" in line:
                for match in re.finditer(r'/Separation/([A-Za-z0-9_-]+)', line):
                    name = match.group(1)
                    if name not in result["spot_colors"]:
                        result["spot_colors"].append(name)

        # Page info via mutool
        pages_result = subprocess.run(
            ["mutool", "show", str(tmp_path), "pages"],
            capture_output=True, text=True
        )
        result["pages_raw"] = pages_result.stdout.strip()

        # Image info via mutool info
        info_result = subprocess.run(
            ["mutool", "info", str(tmp_path)],
            capture_output=True, text=True
        )
        for line in info_result.stdout.splitlines():
            line = line.strip()
            if line and ("bpc" in line or "Image" in line):
                result["images"].append(line)

        # Overprint info via pikepdf
        try:
            with pikepdf.open(str(tmp_path)) as pdf:
                for page_idx, page in enumerate(pdf.pages):
                    page_info = {"page": page_idx + 1}
                    mb = page.get("/MediaBox")
                    if mb:
                        page_info["mediabox"] = [float(v) for v in mb]
                        w_pt, h_pt = float(mb[2]) - float(mb[0]), float(mb[3]) - float(mb[1])
                        page_info["size_mm"] = [round(w_pt / 72 * 25.4, 1), round(h_pt / 72 * 25.4, 1)]
                    res = page.get("/Resources", {})
                    gs = res.get("/ExtGState", {})
                    overprint_states = {}
                    for gs_name, gs_obj in gs.items():
                        op = gs_obj.get("/OP")
                        if op is not None:
                            overprint_states[str(gs_name)] = {
                                "OP": bool(op), "op": bool(gs_obj.get("/op", False)),
                                "OPM": int(gs_obj.get("/OPM", 0))
                            }
                    if overprint_states:
                        page_info["overprint"] = overprint_states
                    result["pages"].append(page_info)
        except Exception as e:
            result["pikepdf_error"] = str(e)

        # Render previews with Ghostscript
        preview_dir = TMP_DIR / f"debug-{uuid.uuid4().hex}"
        preview_dir.mkdir(exist_ok=True)

        # No-overprint preview (png16m)
        no_op_pattern = str(preview_dir / "no_overprint_p%d.png")
        subprocess.run(
            ["gs", "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
             "-sDEVICE=png16m", f"-r{dpi}",
             f"-o{no_op_pattern}", str(tmp_path)],
            capture_output=True, text=True
        )
        pages_b64 = []
        for png in sorted(preview_dir.glob("no_overprint_p*.png")):
            with open(png, "rb") as f:
                pages_b64.append(base64.b64encode(f.read()).decode())
        result["previews"]["no_overprint"] = pages_b64

        # Overprint preview via tiffsep composite (works regardless of OCGs)
        sep_pattern = str(preview_dir / "sep_p%d.tif")
        subprocess.run(
            ["gs", "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
             "-sDEVICE=tiffsep", f"-r{dpi}",
             f"-o{sep_pattern}", str(tmp_path)],
            capture_output=True, text=True
        )
        pages_b64 = []
        # The composite TIFF has the page index suffix without the channel name
        composite_files = sorted([
            p for p in preview_dir.glob("sep_p*.tif")
            if "(" not in p.name
        ])
        for tif in composite_files:
            try:
                pil_img = Image.open(str(tif))
                buf = io.BytesIO()
                pil_img.convert("RGB").save(buf, format="PNG")
                pages_b64.append(base64.b64encode(buf.getvalue()).decode())
            except Exception:
                pass
        result["previews"]["overprint"] = pages_b64

        # Ink analysis: detect non-white pixels in transparent areas of base image
        result["ink_analysis"] = None
        try:
            with pikepdf.open(str(tmp_path)) as pdf:
                page = pdf.pages[0]
                res = page.get("/Resources", {})
                xobjects = res.get("/XObject", {})

                # Also check inside Form XObjects (TPL0 wrappers)
                def find_images(xobjs):
                    base_img = None
                    spot_img = None
                    for name, obj in xobjs.items():
                        subtype = str(obj.get("/Subtype", ""))
                        if subtype == "/Form" and "/Resources" in obj:
                            nested = obj["/Resources"].get("/XObject", {})
                            b, s = find_images(nested)
                            if b: base_img = b
                            if s: spot_img = s
                        elif subtype == "/Image":
                            cs = str(obj.get("/ColorSpace", ""))
                            if "/Separation" in cs:
                                spot_img = obj
                            elif "/DeviceCMYK" in cs or "/DeviceRGB" in cs:
                                base_img = obj
                    return base_img, spot_img

                base_obj, spot_obj = find_images(xobjects)

                if base_obj is not None and spot_obj is not None:
                    base_w = int(base_obj["/Width"])
                    base_h = int(base_obj["/Height"])
                    spot_w = int(spot_obj["/Width"])
                    spot_h = int(spot_obj["/Height"])

                    base_cs = str(base_obj.get("/ColorSpace", ""))
                    has_smask = "/SMask" in base_obj
                    if "/DeviceCMYK" in base_cs:
                        channels = 4
                        mode = "CMYK"
                    else:
                        channels = 3
                        mode = "RGB"

                    # Extract image data, handling JPEG (DCTDecode) streams
                    def extract_image_data(obj, expected_channels):
                        filters = str(obj.get("/Filter", ""))
                        if "/DCTDecode" in filters:
                            raw = obj.read_raw_bytes()
                            pil_img = Image.open(io.BytesIO(raw))
                            return np.array(pil_img)
                        else:
                            data = obj.read_bytes()
                            w = int(obj["/Width"])
                            h = int(obj["/Height"])
                            if len(data) == w * h * expected_channels:
                                return np.frombuffer(data, dtype=np.uint8).reshape(h, w, expected_channels)
                            elif len(data) == w * h:
                                return np.frombuffer(data, dtype=np.uint8).reshape(h, w)
                            return None

                    base_arr = extract_image_data(base_obj, channels)
                    spot_arr = extract_image_data(spot_obj, 1)

                    # Extract SMask if present
                    smask_arr = None
                    if has_smask:
                        smask_obj = base_obj["/SMask"]
                        smask_arr = extract_image_data(smask_obj, 1)
                        if smask_arr is not None and len(smask_arr.shape) == 3:
                            smask_arr = smask_arr.squeeze(axis=2)

                    if base_arr is not None and spot_arr is not None:
                        if len(spot_arr.shape) == 2:
                            pass  # already (h, w)
                        elif spot_arr.shape[2] == 1:
                            spot_arr = spot_arr.squeeze(axis=2)
                        # Resize spot to match base if needed
                        if (spot_h, spot_w) != (base_h, base_w):
                            spot_pil = Image.fromarray(spot_arr, "L").resize((base_w, base_h), Image.NEAREST)
                            spot_arr = np.array(spot_pil)

                        # Transparent mask: where spot alpha is 0
                        transparent_mask = spot_arr == 0

                        if mode == "CMYK":
                            # Any CMYK channel > 0 in transparent area = ink where there shouldn't be
                            ink_sum = base_arr.sum(axis=2)
                        else:
                            # RGB: non-white means ink (255,255,255 = white = no ink)
                            ink_sum = (255 * channels) - base_arr.sum(axis=2).astype(np.int32)
                            ink_sum = np.clip(ink_sum, 0, 255 * channels)

                        # If SMask exists, pixels masked out (alpha=0) are safe regardless of pixel data
                        if smask_arr is not None:
                            if (smask_arr.shape[0], smask_arr.shape[1]) != (base_arr.shape[0], base_arr.shape[1]):
                                smask_pil = Image.fromarray(smask_arr, "L").resize((base_arr.shape[1], base_arr.shape[0]), Image.NEAREST)
                                smask_arr = np.array(smask_pil)
                            masked_out = smask_arr == 0
                            ink_sum[masked_out] = 0  # SMask hides these pixels, no ink problem

                        bad_pixels = transparent_mask & (ink_sum > 0)
                        total_transparent = int(transparent_mask.sum())
                        total_bad = int(bad_pixels.sum())

                        analysis = {
                            "base_colorspace": mode,
                            "base_has_smask": has_smask,
                            "base_size": f"{base_w}x{base_h}",
                            "spot_size": f"{spot_w}x{spot_h}",
                            "transparent_pixels": total_transparent,
                            "ink_in_transparent": total_bad,
                            "has_problem": total_bad > 0,
                        }
                        if total_transparent > 0:
                            analysis["percent_bad"] = round(total_bad / total_transparent * 100, 1)

                        # Base image preview (with SMask applied if present)
                        try:
                            if mode == "CMYK":
                                base_pil = Image.fromarray(base_arr, "CMYK").convert("RGB")
                            else:
                                base_pil = Image.fromarray(base_arr, "RGB")
                            if smask_arr is not None:
                                alpha_pil = Image.fromarray(smask_arr, "L")
                                base_rgba = base_pil.convert("RGBA")
                                base_rgba.putalpha(alpha_pil)
                                base_pil = base_rgba
                            max_dim = 800
                            if max(base_pil.size) > max_dim:
                                ratio = max_dim / max(base_pil.size)
                                base_pil = base_pil.resize(
                                    (int(base_pil.size[0] * ratio), int(base_pil.size[1] * ratio)),
                                    Image.LANCZOS,
                                )
                            buf_base = io.BytesIO()
                            base_pil.save(buf_base, format="PNG")
                            analysis["base_preview"] = base64.b64encode(buf_base.getvalue()).decode()
                        except Exception:
                            pass

                        # Generate heatmap visualization
                        heatmap = np.zeros((base_h, base_w, 3), dtype=np.uint8)
                        heatmap[:] = [32, 32, 32]  # dark gray bg

                        # Show spot coverage in blue
                        spot_visible = spot_arr > 0
                        heatmap[spot_visible] = [40, 40, 80]

                        # Show bad pixels in red, intensity = ink amount
                        if total_bad > 0:
                            bad_intensity = np.clip(ink_sum, 0, 255).astype(np.uint8)
                            heatmap[bad_pixels, 0] = np.clip(bad_intensity[bad_pixels] + 50, 0, 255)
                            heatmap[bad_pixels, 1] = 0
                            heatmap[bad_pixels, 2] = 0

                        # Show clean transparent in green
                        clean = transparent_mask & ~bad_pixels
                        heatmap[clean] = [0, 60, 0]

                        heatmap_img = Image.fromarray(heatmap, "RGB")
                        # Downscale for transfer
                        max_dim = 800
                        if max(base_w, base_h) > max_dim:
                            ratio = max_dim / max(base_w, base_h)
                            heatmap_img = heatmap_img.resize(
                                (int(base_w * ratio), int(base_h * ratio)), Image.NEAREST
                            )

                        buf = io.BytesIO()
                        heatmap_img.save(buf, format="PNG")
                        analysis["heatmap"] = base64.b64encode(buf.getvalue()).decode()

                        result["ink_analysis"] = analysis
        except Exception as e:
            result["ink_analysis_error"] = str(e)

        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug")
async def debug_page():
    return HTMLResponse(content=DEBUG_HTML)


DEBUG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Spot Color Debugger</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #111; color: #eee; padding: 20px; }
  h1 { font-size: 1.4em; margin-bottom: 16px; }
  h2 { font-size: 1.1em; margin: 16px 0 8px; color: #aaa; }
  .upload-area { border: 2px dashed #444; border-radius: 8px; padding: 40px; text-align: center; cursor: pointer; margin-bottom: 20px; transition: border-color 0.2s; }
  .upload-area:hover, .upload-area.dragover { border-color: #888; }
  .upload-area input { display: none; }
  .info { background: #1a1a1a; border-radius: 6px; padding: 12px 16px; margin-bottom: 12px; }
  .info pre { white-space: pre-wrap; font-size: 13px; color: #ccc; }
  .spot-tags { display: flex; gap: 8px; flex-wrap: wrap; }
  .spot-tag { background: #2a2a2a; border: 1px solid #444; border-radius: 4px; padding: 4px 12px; font-family: monospace; }
  .previews { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 12px; }
  .preview-col { text-align: center; }
  .preview-col img { width: 100%; border-radius: 4px; background: #222; }
  .preview-col .label { font-size: 12px; color: #888; margin-bottom: 4px; }
  .loading { display: none; text-align: center; padding: 40px; color: #888; }
  .loading.active { display: block; }
  .results { display: none; }
  .results.active { display: block; }
  .page-section { margin-bottom: 24px; padding-bottom: 24px; border-bottom: 1px solid #222; }
</style>
</head>
<body>
<h1>PDF Spot Color Debugger</h1>

<div class="upload-area" id="dropzone">
  <p>Drop a PDF here or click to upload</p>
  <input type="file" id="fileInput" accept=".pdf">
</div>

<div class="loading" id="loading">Analyzing PDF...</div>

<div class="results" id="results">
  <h2>Spot Colors</h2>
  <div class="info" id="spotColors"></div>

  <h2>Pages</h2>
  <div class="info" id="pageInfo"></div>

  <h2>Images</h2>
  <div class="info" id="imageInfo"></div>

  <h2>Ink Analysis</h2>
  <div class="info" id="inkAnalysis"></div>
  <div id="heatmapContainer" style="margin-bottom:16px;"></div>

  <h2>Previews</h2>
  <div id="previewContainer"></div>
</div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const loading = document.getElementById('loading');
const results = document.getElementById('results');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('dragover'); handleFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));

async function handleFile(file) {
  if (!file) return;
  loading.classList.add('active');
  results.classList.remove('active');

  const form = new FormData();
  form.append('file', file);
  form.append('dpi', '150');

  try {
    const resp = await fetch('/debug', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Error');
    renderResults(data);
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    loading.classList.remove('active');
  }
}

function renderResults(data) {
  // Spot colors
  const sc = document.getElementById('spotColors');
  if (data.spot_colors.length === 0) {
    sc.innerHTML = '<pre>No spot colors found</pre>';
  } else {
    sc.innerHTML = '<div class="spot-tags">' + data.spot_colors.map(s => '<span class="spot-tag">' + s + '</span>').join('') + '</div>';
  }

  // Pages
  const pi = document.getElementById('pageInfo');
  let pageHtml = '';
  for (const p of data.pages) {
    pageHtml += 'Page ' + p.page;
    if (p.size_mm) pageHtml += ' &mdash; ' + p.size_mm[0] + ' x ' + p.size_mm[1] + ' mm';
    if (p.overprint) {
      for (const [gs, vals] of Object.entries(p.overprint)) {
        pageHtml += '\\n  ' + gs + ': OP=' + vals.OP + ', op=' + vals.op + ', OPM=' + vals.OPM;
      }
    }
    pageHtml += '\\n';
  }
  pi.innerHTML = '<pre>' + pageHtml + '</pre>';

  // Images
  const ii = document.getElementById('imageInfo');
  ii.innerHTML = '<pre>' + (data.images.length ? data.images.join('\\n') : 'No images found') + '</pre>';

  // Previews
  const pc = document.getElementById('previewContainer');
  const pageCount = data.previews.no_overprint ? data.previews.no_overprint.length : 0;
  let html = '';
  for (let i = 0; i < pageCount; i++) {
    html += '<div class="page-section"><p style="color:#888;margin-bottom:8px;">Page ' + (i + 1) + '</p><div class="previews" style="grid-template-columns:repeat(2,1fr);">';
    for (const [mode, label] of [['no_overprint', 'No Overprint'], ['overprint', 'Overprint Simulated']]) {
      const b64 = data.previews[mode] && data.previews[mode][i];
      html += '<div class="preview-col"><div class="label">' + label + '</div>';
      if (b64) html += '<img src="data:image/png;base64,' + b64 + '">';
      else html += '<p style="color:#666">N/A</p>';
      html += '</div>';
    }
    html += '</div></div>';
  }
  pc.innerHTML = html;

  // Ink analysis
  const ia = document.getElementById('inkAnalysis');
  const hc = document.getElementById('heatmapContainer');
  if (data.ink_analysis) {
    const a = data.ink_analysis;
    let status = a.has_problem
      ? '<span style="color:#f44">PROBLEM: ' + a.ink_in_transparent.toLocaleString() + ' pixels have ink in transparent areas (' + a.percent_bad + '%)</span>'
      : '<span style="color:#4f4">OK: No ink in transparent areas</span>';
    ia.innerHTML = '<pre>' + status + '\\n\\nBase image: ' + a.base_size + ' ' + a.base_colorspace
      + '\\nSpot image: ' + a.spot_size
      + '\\nTransparent pixels: ' + a.transparent_pixels.toLocaleString()
      + '\\nInk in transparent: ' + a.ink_in_transparent.toLocaleString()
      + '</pre>';
    let hcHtml = '';
    if (a.base_preview) {
      hcHtml += '<p style="color:#888;font-size:12px;margin:12px 0 4px;">Base image only (spot color hidden) &mdash; what prints <em>under</em> the spot layer:</p>'
        + '<img src="data:image/png;base64,' + a.base_preview + '" style="max-width:100%;border-radius:4px;background:repeating-conic-gradient(#222 0% 25%,#333 0% 50%) 50%/20px 20px;">';
    }
    if (a.heatmap) {
      hcHtml += '<p style="color:#888;font-size:12px;margin:12px 0 4px;">Heatmap: <span style="color:#4f4">green</span> = clean transparent, <span style="color:#f44">red</span> = ink in transparent area, <span style="color:#448">blue</span> = spot coverage</p>'
        + '<img src="data:image/png;base64,' + a.heatmap + '" style="max-width:100%;border-radius:4px;">';
    }
    hc.innerHTML = hcHtml;
  } else if (data.ink_analysis_error) {
    ia.innerHTML = '<pre style="color:#fa0">Error: ' + data.ink_analysis_error + '</pre>';
  } else {
    ia.innerHTML = '<pre style="color:#888">No base + spot image pair found to analyze</pre>';
  }

  results.classList.add('active');
}
</script>
</body>
</html>
"""


@app.get("/optimize-starringyou/{poster_id}")
async def optimize_starringyou_poster(poster_id: str):
    pdf_url = f"https://starringyou-rendering.vandalen.workers.dev/?posterId={poster_id}"
    return await flatten(FlattenRequest(input_pdf=pdf_url, dpi=300))


@app.get("/optimize-starringyou-gs/{poster_id}")
async def optimize_starringyou_gs_poster(poster_id: str):
    try:
        pdf_url = f"https://starringyou-rendering.vandalen.workers.dev/?posterId={poster_id}"
        input_path = _resolve_input_path(pdf_url, ".pdf")
        output_path = TMP_DIR / f"optimized-{uuid.uuid4().hex}.pdf"
        result = subprocess.run([
            "gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/prepress",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            f"-sOutputFile={output_path}",
            str(input_path),
        ], capture_output=True, text=True)
        if result.returncode != 0:
            return JSONResponse(status_code=500, content={
                "step": "ghostscript-optimize",
                "error": "Ghostscript optimization failed",
                "stderr": result.stderr,
                "stdout": result.stdout,
            })
        return FileResponse(path=str(output_path), media_type="application/pdf", filename="optimized.pdf")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
