"""Reconciler strings: run progress, parser warnings, evidence notes.
Merged into the app-wide catalog by app.i18n — see that module for the
translation contract (English-source keys, whole-message patterns).
"""
from __future__ import annotations

import re  # noqa: F401  (patterns)

ZH: dict[str, str] = {
    # runtime progress / phases (stored in English, translated at render) ---
    "Parsing workbooks…": "正在解析工作簿…",
    "Adjudicating residue with Claude…": "正在让 Claude 复核剩余存疑行…",
    "Drafting run summary…": "正在撰写核对摘要…",
    "Run complete.": "核对完成。",
    "Run interrupted by a restart — use Retry.": "核对因服务重启而中断——请点「重试」。",
    "Waiting for a free run slot…": "正在排队，等待可用的核对名额…",
    "Working…": "处理中…",
    "starting": "正在启动",
    "parse": "解析文件",
    "resolve": "解析链接",
    "match": "逐行匹配",
    "adjudicate": "复核存疑行",
    "summary": "生成摘要",
    "done": "完成",
    "error": "出错",

    # fixed evidence / warning sentences stored at run time -----------------
    "DMR engagement is a first-crawl snapshot (often within hours of posting) and is NOT comparable to PLOG finals — shown as context only, never used for matching.":
        "DMR 的互动数是首次抓取时的快照（往往在发帖后几小时内），与 PLOG 的最终数据不可比——仅作参考展示，绝不用于匹配判断。",
    "PLOG sheet parsed but contained no data rows.":
        "PLOG 工作表解析成功，但没有数据行。",
    "DMR sheet parsed but contained no data rows.":
        "DMR 工作表解析成功，但没有数据行。",
    "Perimeter sheet parsed but had no data rows.":
        "Perimeter 工作表解析成功，但没有数据行。",
    "Could not parse the DMR export date window from the metadata rows; out-of-window checks are disabled for this run.":
        "无法从元信息行解析出 DMR 导出的日期窗口；本次核对不做「窗口外」检查。",
    "DMR has no 'Username' column — blogger-presence checks (无博主 vs 无帖子) cannot be decided deterministically for this file.":
        "DMR 文件没有「Username」列——该文件无法确定地判断博主有没有被 DMR 收录（无博主 vs 无帖子）。",
    "DMR 'Username' column is entirely empty — blogger-presence checks (无博主 vs 无帖子) cannot be decided deterministically for this file.":
        "DMR 的「Username」列全为空——该文件无法确定地判断博主有没有被 DMR 收录（无博主 vs 无帖子）。",
    "Could not find 'Date of extraction' in the metadata rows — perimeter staleness cannot be shown.":
        "在元信息行中找不到「Date of extraction」——无法显示 Perimeter 名单的提取日期。",
    "The perimeter file recorded for this run is no longer in the cache — running without the perimeter split.":
        "这条核对原先记录的 Perimeter 文件已不在缓存中——本次不做 Perimeter 拆分。",
}

ZH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^Resolving links (\d+/\d+)…$"), r"正在解析链接 \1…"),
    (re.compile(r"^Matching rows (\d+/\d+)…$"), r"正在逐行匹配 \1…"),
    (re.compile(r"^Run failed: (.*)$", re.S), r"核对失败：\1"),
    (re.compile(r"^PLOG row (\d+): POST DATE (.+) could not be parsed — "
                r"date-based checks are skipped for this row\.$"),
     r"PLOG 表第 \1 行：POST DATE \2 无法解析——该行跳过所有基于日期的检查。"),
    (re.compile(r"^Duplicate row identity \(CAMPAIGN=(.+), NO=(.+)\) at sheet "
                r"row (\d+) — each row is still annotated individually \(rows "
                r"are tracked by sheet row\), but check the source data\.$"),
     r"表第 \3 行的行标识重复（CAMPAIGN=\1，NO=\2）——每行仍按表格行号单独标注，但请核查源数据。"),
    (re.compile(r"^PLOG: (\d+) rows in total had unparseable POST DATE values\.$"),
     r"PLOG：共 \1 行的 POST DATE 无法解析。"),
    (re.compile(r"^DMR row (\d+): PostID (.+) is not a 24-char hex note id — "
                r"this row cannot join against resolved links\.$"),
     r"DMR 表第 \1 行：PostID \2 不是 24 位十六进制的笔记 ID——该行无法与解析出的链接做关联。"),
    (re.compile(r"^DMR row (\d+): Link hyperlink embeds PostID (.+) but the "
                r"PostID column says (.+) — using the PostID column for the join\.$"),
     r"DMR 表第 \1 行：Link 超链接内嵌的 PostID 是 \2，而 PostID 列写的是 \3——关联时以 PostID 列为准。"),
    (re.compile(r"^DMR: (\d+) rows in total had non-hex PostID values\.$"),
     r"DMR：共 \1 行的 PostID 不是十六进制格式。"),
    (re.compile(r"^PLOG parse failed: no sheet has a header row containing "
                r"both 'NAME' and 'POST LINK' within the first (\d+) rows\.$"),
     r"PLOG 解析失败：所有工作表的前 \1 行里都找不到同时包含「NAME」和「POST LINK」的表头行。"),
    (re.compile(r"^DMR parse failed: no sheet has a header row containing "
                r"both 'Blogger' and 'PostID' within the first (\d+) rows\.$"),
     r"DMR 解析失败：所有工作表的前 \1 行里都找不到同时包含「Blogger」和「PostID」的表头行。"),
    (re.compile(r"^Perimeter parse failed: no 'List Micro' sheet found "
                r"\(sheets: (.+)\)$", re.S),
     r"Perimeter 解析失败：找不到「List Micro」工作表（现有工作表：\1）。"),
    (re.compile(r"^Perimeter parse failed: no header row containing both "
                r"'NAME' and 'REDBOOK_ID' within the first (\d+) rows of (.+)$",
                re.S),
     r"Perimeter 解析失败：\2 的前 \1 行里找不到同时包含「NAME」和「REDBOOK_ID」的表头行。"),
    # per-row evidence notes (matcher.py / adjudicator.py) ------------------
    (re.compile(r"^PLOG POST DATE (.+) is outside the DMR export window "
                r"(.+)\.\.(.+) — an absent post is expected-missing, not a "
                r"DMR gap\.$"),
     r"PLOG 的 POST DATE \1 在 DMR 导出窗口 \2～\3 之外——查不到帖子属于预期缺失，而不是 DMR 漏抓。"),
    (re.compile(r"^Note-ID join is certain, but DMR records the blogger as "
                r"(.+) which does not contain PLOG name (.+)\.$"),
     r"笔记 ID 关联无疑，但 DMR 里登记的博主名是 \1，并不包含 PLOG 的名字 \2。"),
    (re.compile(r"^DMR tracks author (\S+) \((.+), (\d+) post\(s\)\) but this "
                r"note (\S+) is not among them\.$"),
     r"DMR 在跟踪作者 \1（\2，共 \3 篇），但这条笔记 \4 不在其中。"),
    (re.compile(r"^Link dead/unresolvable, so the note id is unverifiable — "
                r"Tier 3 only ranks same-name candidates; it never asserts a "
                r"match\. Best candidate: (.+) \((\S+)\) Δ=(.+) days\.$"),
     r"链接失效或无法解析，笔记 ID 无从核实——Tier 3 只对同名候选排序，绝不断言匹配。最接近的候选：\1（\2），日期差 \3 天。"),
    (re.compile(r"^Blogger is inside DMR's monitored Micro perimeter "
                r"\(REDBOOK_ID (\S+)\) yet absent from the export — a genuine "
                r"DMR gap, grouped with 无帖子\.$"),
     r"博主在 DMR 监测的 Micro Perimeter 名单内（REDBOOK_ID \1），导出文件里却没有——属于真正的 DMR 漏抓，与「无帖子」同类处理。"),
    (re.compile(r"^同名Perimeter条目但REDBOOK_ID不同（近似未命中）/ same-name "
                r"perimeter entry carries a different REDBOOK_ID "
                r"\((\S+) vs resolved (\S+)\)$"),
     r"同名Perimeter条目但REDBOOK_ID不同（近似未命中）：名单登记 \1，实际解析出 \2。"),
    (re.compile(r"^(\d+)个同名Perimeter条目，无法按名字判定 / name matches "
                r"multiple perimeter rows — never auto-picked by name$"),
     r"\1个同名Perimeter条目，无法按名字判定——绝不按名字自动选取。"),
    (re.compile(r"^match_row failed: (.*)$", re.S), r"该行匹配失败：\1"),
    (re.compile(r"^LLM adjudication unavailable: (.*)$", re.S),
     r"LLM 复核不可用：\1"),
]
