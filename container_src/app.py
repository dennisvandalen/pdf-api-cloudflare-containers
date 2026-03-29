from fastapi import FastAPI, HTTPException
from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
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
import uuid
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


@app.get("/optimize-starringyou/{poster_id}")
async def optimize_starringyou_poster(poster_id: str):
    pdf_url = f"https://starringyou-rendering.vandalen.workers.dev/?posterId={poster_id}"
    return await flatten(FlattenRequest(input_pdf=pdf_url, dpi=300))
