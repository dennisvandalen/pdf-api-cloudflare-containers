"""One print and its spot layers, merged into a single PDF.

The printmijnstad renderer draws a design twice: the print as a PNG, and each
finish — gold foil, silver foil — as its own PDF, printed by the browser and
therefore vector, text included. This turns those into the file a printer
gets: the print as an image, each layer above it in a separation of its own,
overprinting, each on a layer the supplier can switch off in a viewer.

Why every paint in a layer is rewritten rather than converted: the browser
draws in RGB because that is all it can do, and what colour a foil layer was
drawn in means nothing — it is one ink at full strength or it is not there.
So every fill and stroke becomes tint 1.0 of the separation.

What it refuses, and why it is worth refusing: foil is cut from a sheet and
pressed on. It cannot be a photograph, and it cannot be half on. A raster
image or a transparency in a layer means something was drawn that cannot be
made, and the useful moment to say so is now rather than on the press.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import zlib

import pikepdf
from pikepdf import Name, Operator, Pdf
from PIL import Image

MM = 72 / 25.4

# Every operator that sets a colour, and whether it is the stroking one.
COLOUR_OPERATORS = {
    "g": False, "rg": False, "k": False, "sc": False, "scn": False, "cs": False,
    "G": True, "RG": True, "K": True, "SC": True, "SCN": True, "CS": True,
}


class Refused(Exception):
    """The artwork is not something that can be made this way."""


@dataclass(frozen=True)
class Layer:
    """One finish: what it is called, its ink, and the file drawn for it."""

    name: str
    spot: str
    fallback_cmyk: tuple
    pdf: Path

    @staticmethod
    def parse(spec: dict, files: dict) -> "Layer":
        name = str(spec.get("name") or "")
        if name not in files:
            raise Refused(f"no file was sent for layer {name or '(unnamed)'}")

        fallback = spec.get("fallback_cmyk") or [0, 0, 0, 1]
        if len(fallback) != 4 or not all(0 <= float(v) <= 1 for v in fallback):
            raise Refused(f"layer {name}: fallback_cmyk is four numbers from 0 to 1")

        return Layer(
            name=name,
            spot=str(spec.get("spot") or name),
            fallback_cmyk=tuple(float(v) for v in fallback),
            pdf=files[name],
        )


@dataclass(frozen=True)
class Page:
    """The sheet in millimetres: what is trimmed, and what is trimmed off."""

    width_mm: float
    height_mm: float
    bleed_mm: float = 0.0

    @property
    def width(self) -> float:
        return (self.width_mm + self.bleed_mm * 2) * MM

    @property
    def height(self) -> float:
        return (self.height_mm + self.bleed_mm * 2) * MM

    @property
    def trim(self) -> list:
        inset = self.bleed_mm * MM

        return [inset, inset, self.width - inset, self.height - inset]

    @staticmethod
    def parse(spec: dict) -> "Page":
        try:
            page = Page(
                width_mm=float(spec["width_mm"]),
                height_mm=float(spec["height_mm"]),
                bleed_mm=float(spec.get("bleed_mm") or 0),
            )
        except (KeyError, TypeError, ValueError):
            raise Refused("the page needs a width_mm and a height_mm")

        if page.width_mm <= 0 or page.height_mm <= 0:
            raise Refused("the page has no size")

        return page


def recolour(operations: Iterable, spot: str) -> list:
    """Every paint in a content stream set to tint 1.0 of the separation.

    Parsed into operands and operators rather than matched in the raw bytes:
    a pattern like "number then g" also hits the g of a word inside a text
    string, and there is no reading a content stream without parsing it.
    """
    out = []

    for operands, operator in operations:
        name = str(operator)

        if name in ("BI", "ID", "EI"):
            raise Refused("the artwork holds an inline image")

        if name in COLOUR_OPERATORS:
            stroking = COLOUR_OPERATORS[name]
            out.append(([Name(f"/{spot}")], Operator("CS" if stroking else "cs")))
            out.append(([1], Operator("SCN" if stroking else "scn")))
            continue

        out.append((operands, operator))

    return out


def check(resources) -> None:
    """Refuse what cannot be made in one ink, before anything is rewritten."""
    for name, state in dict(resources.get("/ExtGState", {})).items():
        for key in ("/ca", "/CA"):
            if key in state and float(state[key]) < 1:
                raise Refused(f"{name} draws at {float(state[key])} opacity")
        if state.get("/SMask", Name("/None")) != Name("/None"):
            raise Refused(f"{name} carries a soft mask")

    for name, xobject in dict(resources.get("/XObject", {})).items():
        if xobject.get("/Subtype") == Name("/Image"):
            raise Refused(f"{name} is a raster image")


def separation(pdf: Pdf, layer: Layer):
    """The ink itself, and what a viewer without it should show instead."""
    tint = pikepdf.Dictionary(
        FunctionType=2,
        Domain=[0, 1],
        C0=[0, 0, 0, 0],
        C1=list(layer.fallback_cmyk),
        N=1,
    )

    return pdf.make_indirect(pikepdf.Array([
        Name("/Separation"), Name(f"/{layer.spot}"), Name("/DeviceCMYK"), tint,
    ]))


def overprinting(pdf: Pdf):
    """No knockout: the print underneath stays whole and the foil goes on top."""
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=Name("/ExtGState"), OP=True, op=True, OPM=1, CA=1, ca=1,
    ))


def paint_with(pdf: Pdf, container, spot: str, ink, state, seen: set) -> None:
    """A content stream, everything it draws through, and its own resources.

    Form XObjects are followed because Chrome nests most of a page in one,
    and Type 3 fonts because it embeds a variable font's glyphs as little
    content streams that each set their own colour — a title left out of the
    foil is a title that is simply missing from the board.
    """
    resources = container.get("/Resources")
    if resources is None:
        resources = pikepdf.Dictionary()
        container["/Resources"] = resources

    check(resources)

    for key, value, alias in (("/ColorSpace", ink, spot), ("/ExtGState", state, "GsSpot")):
        if key not in resources:
            resources[key] = pikepdf.Dictionary()
        resources[key][f"/{alias}"] = value

    for _, xobject in dict(resources.get("/XObject", {})).items():
        if xobject.get("/Subtype") == Name("/Form") and xobject.objgen not in seen:
            seen.add(xobject.objgen)
            paint_with(pdf, xobject, spot, ink, state, seen)

    for _, font in dict(resources.get("/Font", {})).items():
        if font.get("/Subtype") != Name("/Type3"):
            continue
        for _, proc in dict(font.get("/CharProcs", {})).items():
            if proc.objgen in seen:
                continue
            seen.add(proc.objgen)
            proc.write(pikepdf.unparse_content_stream(
                recolour(pikepdf.parse_content_stream(proc), spot)
            ))

    prologue = [
        ([Name("/GsSpot")], Operator("gs")),
        ([Name(f"/{spot}")], Operator("cs")),
        ([1], Operator("scn")),
        ([Name(f"/{spot}")], Operator("CS")),
        ([1], Operator("SCN")),
    ]
    body = pikepdf.unparse_content_stream(
        prologue + recolour(pikepdf.parse_content_stream(container), spot)
    )

    if isinstance(container, pikepdf.Page):
        container.contents_coalesce()
        container.obj["/Contents"] = pdf.make_stream(body)
    else:
        container.write(body)


def as_spot(layer: Layer, target: Path) -> Path:
    """One browser-printed layer, rewritten into its separation."""
    with Pdf.open(layer.pdf) as pdf:
        if len(pdf.pages) != 1:
            raise Refused(f"layer {layer.name} is {len(pdf.pages)} pages, not one")

        page = pdf.pages[0]
        paint_with(pdf, page, layer.spot, separation(pdf, layer), overprinting(pdf), set())
        pdf.save(target)

    return target


def image_xobject(pdf: Pdf, png: Path):
    """The print, as an image at whatever resolution it was captured at.

    Kept in RGB rather than converted: the print is separated downstream by
    whoever prints it, and a conversion here would be a guess at their
    profile.
    """
    with Image.open(png) as opened:
        image = opened.convert("RGB")
        width, height = image.size
        raw = image.tobytes()

    return pdf.make_stream(zlib.compress(raw, 6), pikepdf.Dictionary(
        Type=Name("/XObject"),
        Subtype=Name("/Image"),
        Width=width,
        Height=height,
        ColorSpace=Name("/DeviceRGB"),
        BitsPerComponent=8,
        Filter=Name("/FlateDecode"),
    ))


def group(pdf: Pdf, label: str):
    """One switchable layer in a viewer, for the print and for each finish."""
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=Name("/OCG"), Name=pikepdf.String(label),
    ))


def build(spec: dict, base: Path | None, files: dict, target: Path) -> Path:
    """The print and its finishes as one PDF, in the order they are made."""
    page = Page.parse(spec.get("page") or {})
    layers = [Layer.parse(one, files) for one in (spec.get("layers") or [])]
    if not layers:
        raise Refused("a file with no layer on it is the print itself")

    pdf = Pdf.new()
    sheet = pdf.add_blank_page(page_size=(page.width, page.height))
    groups = []

    if base is not None:
        printed = image_xobject(pdf, base)
        printed["/OC"] = group(pdf, "Print")
        groups.append(printed["/OC"])
        name = sheet.add_resource(printed, Name("/XObject"), Name("/Print"))
        placed = f"q {page.width} 0 0 {page.height} 0 0 cm {name} Do Q\n".encode()
    else:
        placed = b""

    for layer in layers:
        spot = as_spot(layer, target.with_name(f"{target.stem}-{layer.name}.pdf"))

        with Pdf.open(spot) as drawn:
            form = pdf.copy_foreign(drawn.pages[0].as_form_xobject())

        form["/OC"] = group(pdf, layer.name)
        groups.append(form["/OC"])
        name = sheet.add_resource(form, Name("/XObject"))

        # Anchored top-left, because that is where the browser laid the
        # artwork out and its page is a rounding larger than the sheet.
        box = [float(v) for v in drawn_media(spot)]
        placed += (
            f"q 1 0 0 1 {-box[0]} {page.height - box[3]} cm {name} Do Q\n".encode()
        )

    sheet.obj["/Contents"] = pdf.make_stream(placed)

    for key in ("/MediaBox", "/BleedBox", "/CropBox"):
        sheet.obj[key] = pikepdf.Array([0, 0, page.width, page.height])
    sheet.obj["/TrimBox"] = pikepdf.Array(page.trim)

    if (spec.get("output") or {}).get("ocg", True) and groups:
        pdf.Root["/OCProperties"] = pikepdf.Dictionary(
            OCGs=pikepdf.Array(groups),
            D=pikepdf.Dictionary(Order=pikepdf.Array(groups), ON=pikepdf.Array(groups)),
        )

    pdf.save(target)

    return target


def drawn_media(pdf_path: Path) -> list:
    """A page's own box, which says where its artwork was laid out."""
    with Pdf.open(pdf_path) as pdf:
        return [float(v) for v in pdf.pages[0].mediabox]
