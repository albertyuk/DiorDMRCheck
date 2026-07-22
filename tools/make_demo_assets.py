"""Build the /static efficiency-demo images from SYNTHETIC data.

Real workbooks are client campaign data and must never ship as site assets;
this dataset is invented but shaped like a real wave (all 8 TIER × 报备/软植
groups, realistic XHS price/impression ranges, one viral-post concentration
in KOC SOFT so the on-slide caveat machinery is visible in the demo).
"""
import io
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openpyxl import Workbook

from app.deck import assert_chart_cache, build_deck
from app.effreport import ReportConfig, analyze

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
                       f"http://xhslink.com/demo{no:03d}", impr, like, coll,
                       comm, eng, price, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SCRATCH = Path(tempfile.mkdtemp(prefix="effdemo-"))
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
data = build_demo_bytes()

for lang in ("en", "zh"):
    a = analyze(io.BytesIO(data), ReportConfig(language=lang))
    assert not a["blocked"], a["findings"]
    print(lang, "findings:", [(f["code"], f["message"][:60]) for f in a["findings"]])
    pptx = build_deck(a)
    assert_chart_cache(pptx, a)
    p = SCRATCH / f"demo_{lang}.pptx"
    p.write_bytes(pptx)
    subprocess.run(["python", "/root/.claude/skills/pptx/scripts/office/soffice.py",
                    "--headless", "--convert-to", "pdf", "--outdir", str(SCRATCH),
                    str(p)], check=True, capture_output=True)
    subprocess.run(["pdftoppm", "-jpeg", "-r", "150",
                    str(SCRATCH / f"demo_{lang}.pdf"),
                    str(SCRATCH / f"demo_{lang}")], check=True)

from PIL import Image
for lang in ("en", "zh"):
    src = SCRATCH / f"demo_{lang}-1.jpg"
    img = Image.open(src)
    img.thumbnail((1200, 1200), Image.LANCZOS)
    out = STATIC / f"eff_demo_{lang}.jpg"
    img.save(out, "JPEG", quality=87, optimize=True)
    print(out, img.size, f"{out.stat().st_size // 1024}KB")
