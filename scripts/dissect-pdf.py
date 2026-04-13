#!/usr/bin/env python3
"""Dissect a PDF: show structure and extract image/spot-color layers as separate files."""

import re
import sys
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject


def find_xobjects(resources, prefix=""):
    """Recursively find all image XObjects, including inside Form XObjects."""
    results = []
    xobjects = resources.get("/XObject", {})
    for name, obj in xobjects.items():
        subtype = str(obj.get("/Subtype", ""))
        if subtype == "/Image":
            results.append((f"{prefix}{name}", obj))
        elif subtype == "/Form" and "/Resources" in obj:
            results.extend(find_xobjects(obj["/Resources"], prefix=f"{prefix}{name}/"))
    return results


def classify_image(obj):
    """Classify an image XObject as spot, cmyk, rgb, or other."""
    cs = obj.get("/ColorSpace")
    if cs is None:
        return "unknown", None
    cs_str = str(cs)
    if "/Separation" in cs_str:
        # Extract spot color name
        if isinstance(cs, ArrayObject) and len(cs) >= 2:
            return "spot", str(cs[1])
        return "spot", "unknown"
    if cs_str == "/DeviceCMYK":
        return "cmyk", None
    if cs_str == "/DeviceRGB":
        return "rgb", None
    if cs_str == "/DeviceGray":
        return "gray", None
    return "other", cs_str


def dissect(pdf_path):
    path = Path(pdf_path)
    reader = PdfReader(str(path))
    stem = path.stem
    out_dir = path.parent

    print(f"--- {path.name} ---")
    print(f"Pages: {len(reader.pages)}")

    for page_idx, page in enumerate(reader.pages):
        mb = page.get("/MediaBox", [])
        print(f"\nPage {page_idx + 1}: MediaBox={list(mb)}")

        resources = page.get("/Resources", {})

        # Overprint
        ext_gstate = resources.get("/ExtGState", {})
        for gs_name, gs in ext_gstate.items():
            op = gs.get("/OP")
            opm = gs.get("/OPM")
            if op is not None:
                print(f"  ExtGState {gs_name}: OP={op}, op={gs.get('/op')}, OPM={opm}")

        # Images
        images = find_xobjects(resources)
        if not images:
            print("  No image XObjects found")
            continue

        for name, obj in images:
            kind, detail = classify_image(obj)
            w = obj.get("/Width", "?")
            h = obj.get("/Height", "?")
            bpc = obj.get("/BitsPerComponent", "?")
            smask = "yes" if "/SMask" in obj else "no"

            label = kind.upper()
            if detail:
                label += f" ({detail})"

            print(f"  {name}: {label}, {w}x{h}, BPC={bpc}, SMask={smask}")

    # Extract layers into separate single-page PDFs
    print(f"\nExtracting layers to {out_dir}/")
    page = reader.pages[0]
    resources = page.get("/Resources", {})
    images = find_xobjects(resources)

    for name, obj in images:
        kind, detail = classify_image(obj)
        clean_name = name.strip("/").replace("/", "-")

        if kind == "spot":
            suffix = f"spot-{detail.strip('/')}" if detail else "spot"
        elif kind in ("cmyk", "rgb"):
            suffix = f"base-{kind}"
        else:
            suffix = f"layer-{clean_name}"

        out_path = out_dir / f"{stem}-{suffix}.pdf"

        # Build a minimal PDF with just this image
        writer = PdfWriter()
        writer.append(reader, [0])
        extract_page = writer.pages[0]

        # Remove XObjects we don't want and rewrite content stream
        try:
            extract_resources = extract_page["/Resources"]
            xobjects = extract_resources.get("/XObject", {})
            to_remove = [k for k in xobjects if k != name]
            for k in to_remove:
                del xobjects[k]

            # Rewrite content stream: remove Do calls for deleted XObjects
            contents = extract_page.get("/Contents")
            if contents is not None:
                if hasattr(contents, "get_data"):
                    raw = contents.get_data().decode("latin-1")
                else:
                    raw = contents.read().decode("latin-1") if hasattr(contents, "read") else str(contents)

                for k in to_remove:
                    xobj_name = k.strip("/")
                    # Remove "q ... /Name Do ... Q" blocks and bare "/Name Do" lines
                    raw = re.sub(
                        rf'q\s[^Q]*/{re.escape(xobj_name)}\s+Do\s*Q\s*',
                        '', raw
                    )
                    raw = re.sub(
                        rf'/{re.escape(xobj_name)}\s+Do\s*',
                        '', raw
                    )

                from pypdf.generic import DecodedStreamObject, NameObject
                new_contents = DecodedStreamObject()
                new_contents.set_data(raw.encode("latin-1"))
                extract_page[NameObject("/Contents")] = new_contents
        except Exception as e:
            print(f"  Warning: could not clean content stream: {e}")

        writer.write(str(out_path))
        print(f"  {out_path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <pdf_file> [pdf_file ...]")
        sys.exit(1)
    for f in sys.argv[1:]:
        dissect(f)
        print()
