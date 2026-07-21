"""Efficiency-report strings: demo card, V1-V10 findings, insights.
Merged into the app-wide catalog by app.i18n — see that module for the
translation contract (English-source keys, whole-message patterns).
"""
from __future__ import annotations

import re  # noqa: F401  (patterns)

ZH: dict[str, str] = {
    # efficiency demo image (index + efficiency form) -----------------------
    "Sample efficiency slide generated from synthetic demo data":
        "由合成演示数据生成的效率幻灯片示例",
    "Sample output (synthetic demo data)": "示例输出（合成演示数据）",
    "The slide this produces — this sample is built from synthetic demo data, not client data. The donut shows group shares; the bars compare 报备 vs 软植 prices, CPM and CPE per tier, with data caveats printed on the slide.":
        "生成的幻灯片就长这样——本示例由合成演示数据生成，非客户数据。环图展示各分组占比；柱状图按层级对比 报备 vs 软植 的合作价格、CPM 与 CPE，数据风险提示会直接标注在幻灯片上。",
}

ZH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # V1 sheet/columns errors (analysis.py parse_report)
    (re.compile(r"^V1: no header row containing NAME and POST LINK found in "
                r"sheet (.+)\.$"),
     r"V1：工作表 \1 中找不到包含「NAME」和「POST LINK」的表头行。"),
    (re.compile(r"^V1: required columns missing after header normalization: (.+)$",
                re.S),
     r"V1：表头标准化后仍缺少必需列：\1"),
    # efficiency-report validation findings (effreport.py V1–V10) -----------
    (re.compile(r"^Sheet 'MASTER KOL LIST' not found — using first sheet "
                r"(.+)\.$"),
     r"未找到「MASTER KOL LIST」工作表——改用第一个工作表 \1。"),
    (re.compile(r"^Unclassified TYPE value (.+) on (\d+) row\(s\) — excluded "
                r"from groups, counted in totals\.$"),
     r"无法识别的 TYPE 值 \1，共 \2 行——不计入分组，仍计入总量。"),
    (re.compile(r"^Unclassified LEVEL value (.+) on (\d+) row\(s\) — excluded "
                r"from groups, counted in totals\.$"),
     r"无法识别的 LEVEL 值 \1，共 \2 行——不计入分组，仍计入总量。"),
    (re.compile(r"^(\d+) row\(s\) missing IMPRESSION/LIKE/COLLECTION/COMMENT/"
                r"TTL ENGAGEMENT/PRICE values \(missing_row_policy=block\)\.$"),
     r"\1 行缺少 IMPRESSION/LIKE/COLLECTION/COMMENT/TTL ENGAGEMENT/PRICE 值"
     r"（missing_row_policy=block）。"),
    (re.compile(r"^(\d+) row\(s\) missing metric values — excluded from all "
                r"metrics \(missing_row_policy=exclude_warn\)\.$"),
     r"\1 行缺少指标值——已从所有指标中排除（missing_row_policy=exclude_warn）。"),
    (re.compile(r"^(\d+) row\(s\) with IMPRESSION=0 — excluded from CPM "
                r"ratios only\.$"),
     r"\1 行 IMPRESSION=0——仅从 CPM 计算中排除。"),
    (re.compile(r"^(\d+) row\(s\) with TTL ENGAGEMENT=0 — excluded from CPE "
                r"ratios only\.$"),
     r"\1 行 TTL ENGAGEMENT=0——仅从 CPE 计算中排除。"),
    (re.compile(r"^TTL ENGAGEMENT ≠ LIKE\+COLLECTION\+COMMENT on (\d+) "
                r"row\(s\)\.$"),
     r"共 \1 行 TTL ENGAGEMENT ≠ LIKE+COLLECTION+COMMENT。"),
    (re.compile(r"^Source CPM/CPE drift from recomputation on (\d+) row\(s\)\. "
                r"Note the source CPM column is PRICE÷IMPRESSION — cost per "
                r"SINGLE impression, ×1000 off the industry-standard per-mille "
                r"CPM; this report never reuses it\.$"),
     r"共 \1 行源文件的 CPM/CPE 与重新计算的结果有偏差。注意：源文件的 CPM 列是 "
     r"PRICE÷IMPRESSION——单次曝光的成本，与行业标准的每千次 CPM 差 1000 倍；"
     r"本报告从不复用该列。"),
    (re.compile(r"^Duplicate POST LINK shared by (\d+) rows \((.+)\): …(.+) — "
                r"likely a copy-paste error in the source; metrics keep both "
                r"rows, but verify\.$", re.S),
     r"\2 这 \1 行共用同一个 POST LINK：…\3——疑似源数据里的复制粘贴错误；"
     r"指标保留所有行，但请人工核实。"),
    (re.compile(r"^Both 尾部 \((\d+) rows, fans (.+)K\) and 底部 \((\d+) rows, "
                r"fans (.+)K\) labels coexist — merged into BOT \(documented "
                r"judgment call\)\. (\d+) of the (\d+) 底部 accounts have "
                r"<(\d+)K fans \(KOC-sized\)\. Set tier_mode=fanbase to "
                r"re-tier by thresholds instead\.$"),
     r"「尾部」（\1 行，粉丝 \2K）与「底部」（\3 行，粉丝 \4K）两种标签并存——"
     r"已合并为 BOT（既定的判断规则）。\6 个「底部」账号中有 \5 个粉丝不足 \7K"
     r"（KOC 量级）。如需按粉丝量阈值重新分层，层级判定可改选 FAN BASE 阈值。"),
    (re.compile(r"^(.+): n=(\d+) — below min_group_n=(\d+); on-slide caveat "
                r"added \(not a benchmark\)\.$"),
     r"\1：n=\2，低于最小样本量 min_group_n=\3；已在幻灯片上加注（不可作为基准）。"),
    (re.compile(r"^(.+): top (\d+) post\(s\) hold (\d+)% of group impressions "
                r"— pooled CPM is dragged by outliers; plan on per-post "
                r"≈(\d+), not pooled (\d+)\. On-slide caveat added\.$"),
     r"\1：曝光最高的 \2 篇帖子占了组内 \3% 的曝光——合并口径 CPM 被极值拉低；"
     r"实际规划请按单篇平均 ≈\4，而不是合并口径的 \5。已在幻灯片上加注。"),
    # efficiency-report insight bullets / footnote (effreport.py) -----------
    (re.compile(r"^PAID CARRIES A PREMIUM IN EVERY TIER — (.+) VS SOFT$"),
     r"PAID 在每个层级都有溢价——\1（相对 SOFT）"),
    (re.compile(r"^PAID PREMIUM: (.+) VS SOFT$"),
     r"PAID 溢价：\1（相对 SOFT）"),
    (re.compile(r"^PRICE INVERSION — SOFT PRICED ABOVE PAID: (.+)$"),
     r"价格倒挂——SOFT 报价高于 PAID：\1"),
    (re.compile(r"^CPM WINNER — (.+)$"), r"CPM 更优——\1"),
    (re.compile(r"^CPE WINNER — (.+)$"), r"CPE 更优——\1"),
    (re.compile(r"^(.+) = (\d+|\?) POST\(S\) ONLY — NOT A BENCHMARK$"),
     r"\1 仅 \2 篇——样本过小，不可作为基准"),
    (re.compile(r"^CAUTION: (.+) CARRIED BY (\d+) VIRAL POSTS \((\d+)% OF "
                r"GROUP IMPRESSIONS\) — PLAN ON ~¥(\d+) CPM, NOT ¥(\d+)$"),
     r"注意：\1 的数据主要由 \2 篇爆款撑起（占组内曝光 \3%）——规划请按 CPM "
     r"约 ¥\4，而非 ¥\5"),
    (re.compile(r"^(.+): PER-POST CPM ¥(\d+) IS >3× POOLED ¥(\d+) — RESULTS "
                r"CONCENTRATED IN OUTLIERS$"),
     r"\1：单篇平均 CPM ¥\2 超过合并口径 ¥\3 的 3 倍——效果集中在少数极值帖"),
    (re.compile(r"^Basis: pooled — group total spend ÷ total impressions "
                r"\(CPM, ¥ per 1,000\) / total engagements \(CPE\)\. "
                r"n = (\d+) posts: (.+)$"),
     r"口径：合并——组内总花费 ÷ 总曝光（CPM，¥/千次）、÷ 总互动（CPE）。"
     r"n = \1 篇：\2"),
    (re.compile(r"^Basis: per-post average — mean of PRICE÷IMPRESSION×1000 "
                r"\(CPM, ¥ per 1,000\) / PRICE÷ENGAGEMENT \(CPE\) across "
                r"posts\. n = (\d+) posts: (.+)$"),
     r"口径：单篇平均——各帖 PRICE÷IMPRESSION×1000（CPM，¥/千次）、"
     r"PRICE÷ENGAGEMENT（CPE）的平均。n = \1 篇：\2"),
]
