"""What /v1/print/spot-layers must keep doing.

    python render-tests/spot_layers_test.py

The fixtures are real output from the printmijnstad renderer: an A5 sheet
whose gold layer holds a circle, a ring of type set in a TrueType font, a
title set in a Type 3 one, a heart, a rule and a field of stars — so between
them they cover every way a browser puts marks on a page.

What is checked is what a printer would notice: the page is the size that was
asked for, the spot plate carries the artwork, no process plate carries any
of it, and a layer that cannot be made in foil is refused by name.

It shells out to Ghostscript for the separations, as the container has it.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container_src"))

import pikepdf  # noqa: E402
import spot_layers  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = {"width_mm": 148, "height_mm": 210, "bleed_mm": 0}
GOLD = {"name": "foil-gold", "spot": "Gold", "fallback_cmyk": [0, 0.2, 0.6, 0.2]}
MM = 72 / 25.4

failures = []


def check(what: str, holds: bool, detail: str = "") -> None:
    print(f"{'ok  ' if holds else 'FOUT'}  {what}{f' — {detail}' if detail else ''}")
    if not holds:
        failures.append(what)


def plates(pdf: Path, workdir: Path) -> dict:
    """Ink per separation, as a share of the page, straight out of Ghostscript."""
    out = workdir / "sep"
    out.mkdir(exist_ok=True)
    subprocess.run(
        ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=tiffsep", "-r72",
         f"-sOutputFile={out / 'p.tif'}", str(pdf)],
        check=True, capture_output=True,
    )

    from PIL import Image

    inks = {}
    for plate in out.glob("p(*.tif"):
        name = plate.name[len("p("):-len(").tif")]
        with Image.open(plate) as image:
            counts = image.convert("L").histogram()
            total = sum(counts)
            mean = sum(value * count for value, count in enumerate(counts)) / total
            inks[name] = 100 * (1 - mean / 255)

    return inks


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        spec = {"page": PAGE, "layers": [GOLD], "output": {"ocg": True}}
        built = spot_layers.build(
            spec,
            FIXTURES / "print.png",
            {"foil-gold": FIXTURES / "foil-gold-layer.pdf"},
            workdir / "print.pdf",
        )

        with pikepdf.open(built) as pdf:
            page = pdf.pages[0]
            media = [round(float(v), 2) for v in page.mediabox]
            names = [str(one["/Name"]) for one in pdf.Root["/OCProperties"]["/OCGs"]]

        check(
            "the page is the size that was asked for",
            media == [0, 0, round(148 * MM, 2), round(210 * MM, 2)],
            f"{media}",
        )
        check("one layer per finish, and one for the print", names == ["Print", "foil-gold"], f"{names}")

        ink = plates(built, workdir)
        check("the spot plate carries the artwork", ink.get("Gold", 0) > 3, f"Gold {ink.get('Gold', 0):.2f}%")
        check(
            "the print is on the process plates and the spot is not",
            ink.get("Cyan", 0) > 10,
            ", ".join(f"{name} {value:.1f}%" for name, value in sorted(ink.items())),
        )

        # The layer on its own: nothing of it may reach a process plate.
        alone = spot_layers.build(
            {"page": PAGE, "layers": [GOLD]},
            None,
            {"foil-gold": FIXTURES / "foil-gold-layer.pdf"},
            workdir / "layer-only.pdf",
        )
        ink = plates(alone, workdir)
        process = max(ink.get(name, 0) for name in ("Cyan", "Magenta", "Yellow", "Black"))
        check("a layer alone leaves every process plate empty", process == 0, f"hoogste {process:.3f}%")
        check(
            "and the type comes with it: TrueType and Type 3 alike",
            ink.get("Gold", 0) > 5,
            f"Gold {ink.get('Gold', 0):.2f}%",
        )

        for what, spec, files in (
            ("a page without a size", {"layers": [GOLD]}, {}),
            ("a file with no layers", {"page": PAGE, "layers": []}, {}),
            ("a layer with no file", {"page": PAGE, "layers": [GOLD]}, {}),
        ):
            try:
                spot_layers.build(spec, None, files, workdir / "refused.pdf")
                check(f"{what} is refused", False, "it was not")
            except spot_layers.Refused as e:
                check(f"{what} is refused", True, str(e))

    if failures:
        print(f"\n{len(failures)} van de controles faalde")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
