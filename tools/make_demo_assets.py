"""Build the /static efficiency-demo images from SYNTHETIC data.

Real workbooks are client campaign data and must never ship as site assets;
this dataset is invented but shaped like a real wave (all 8 TIER × 报备/软植
groups, realistic XHS price/impression ranges, one viral-post concentration
in KOC SOFT so the on-slide caveat machinery is visible in the demo).
"""
import io
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openpyxl import Workbook
from PIL import Image

from app.efficiency.analysis import ReportConfig, analyze
from app.efficiency.deck import assert_chart_cache, build_deck

HEADERS = ["NO", "MCN", "CAMPAIGN", "TYPE", "LEVEL", "NAME", "FAN BASE（K)",
           "POST DATE", "MICRO MACRO", "POST LINK", "IMPRESSION", "LIKE",
           "COLLECTION", "COMMENT", "TTL  ENGAGEMENT", "PRICE", "CPM", "CPE"]
PAID, SOFT = "报备图文", "软植图文"

# (type, level, fanbase_k, [(price, impressions), ...])
GROUPS = [
    (PAID, "头部", 1400, [(105000, 880000), (95000, 820000), (100000, 760000),
                          (92000, 700000), (108000, 940000)]),
    (SOFT, "头部", 1200, [(62000, 560000), (58000, 500000), (60000, 470000),
                          (55000, 430000), (65000, 610000)]),
    (PAID, "腰部", 600, [(26000, 330000), (24000, 300000), (28000, 360000),
                         (22000, 260000), (25000, 310000), (27000, 340000)]),
    (SOFT, "腰部", 520, [(12000, 210000), (11000, 190000), (13000, 230000),
                         (10000, 170000), (14000, 250000), (12000, 200000)]),
    (PAID, "尾部", 280, [(6500, 100000), (5500, 84000), (6000, 92000),
                         (7000, 108000), (5000, 76000), (6200, 95000),
                         (5800, 88000), (6800, 104000), (5200, 79000),
                         (6300, 97000)]),
    (SOFT, "尾部", 240, [(3200, 62000), (2800, 52000), (3000, 57000),
                         (3400, 66000), (2600, 48000), (3100, 60000),
                         (2900, 54000), (3300, 64000)]),
    (PAID, "KOC", 90, [(1600, 40000), (1400, 34000), (1500, 37000),
                       (1700, 43000), (1300, 31000), (1550, 38000)]),
    # one viral post → top-2 concentration > 50% → visible on-slide caveat
    (SOFT, "KOC", 70, [(900, 118000), (800, 13000), (750, 11000),
                       (850, 14000), (700, 9000), (950, 16000)]),
]


def build_demo_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER KOL LIST"
    ws.append(HEADERS)
    no = 0
    for type_, level, fan, posts in GROUPS:
        for price, impr in posts:
            no += 1
            eng = round(impr * (0.030 + (no % 5) * 0.004))
            like = round(eng * 0.72)
            coll = round(eng * 0.17)
            comm = eng - like - coll
            ws.append([no, "", "DEMO WAVE", type_, level, f"demo{no:02d}",
                       fan + no, datetime(2026, 6, 1 + (no - 1) % 28), "",
                       f"https://xhslink.com/demo{no:03d}", impr, like, coll,
                       comm, eng, price, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _required_tool(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        f"Required executable not found on PATH: {' or '.join(names)}"
    )


def main() -> None:
    office = _required_tool("libreoffice", "soffice")
    pdftoppm = _required_tool("pdftoppm")
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    data = build_demo_bytes()

    with tempfile.TemporaryDirectory(prefix="effdemo-") as tmp:
        scratch = Path(tmp)
        for lang in ("en", "zh"):
            analysis = analyze(
                io.BytesIO(data), ReportConfig(language=lang)
            )
            assert not analysis["blocked"], analysis["findings"]
            print(
                lang,
                "findings:",
                [(f["code"], f["message"][:60])
                 for f in analysis["findings"]],
            )
            pptx = build_deck(analysis)
            assert_chart_cache(pptx, analysis)
            presentation = scratch / f"demo_{lang}.pptx"
            presentation.write_bytes(pptx)
            subprocess.run(
                [
                    office, "--headless", "--convert-to", "pdf",
                    "--outdir", str(scratch), str(presentation),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    pdftoppm, "-jpeg", "-r", "150",
                    str(scratch / f"demo_{lang}.pdf"),
                    str(scratch / f"demo_{lang}"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        for lang in ("en", "zh"):
            src = scratch / f"demo_{lang}-1.jpg"
            out = static / f"eff_demo_{lang}.jpg"
            with Image.open(src) as image:
                image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                image.save(out, "JPEG", quality=87, optimize=True)
                size = image.size
            print(out, size, f"{out.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
