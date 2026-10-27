"""Check local build/review dependencies without reading private configuration."""

import json
import sys
from pathlib import Path

import cairosvg
from PIL import ImageFont, features

from einkrelayer.books.validation import epubcheck


def check():
    results = {
        "python_3_13": sys.version_info[:2] == (3, 13),
        "epubcheck": epubcheck() is not None,
        "rtl_shaping": features.check_feature("raqm"),
        "cjk_fonts": any(
            path.is_file()
            for path in (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path(".cache/fonts/NotoSansCJKsc-Regular.otf"),
            )
        ),
    }
    try:
        cairosvg.svg2png(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
        )
        results["cairo"] = True
    except (OSError, ValueError):
        results["cairo"] = False
    try:
        ImageFont.truetype("DejaVuSans.ttf", 12)
        results["dejavu_fonts"] = True
    except OSError:
        results["dejavu_fonts"] = False
    return results


if __name__ == "__main__":
    results = check()
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if all(results.values()) else 1)
