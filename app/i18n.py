"""UI localization — English default, context-aware Chinese via a header toggle.

Two translators, both keyed by the ENGLISH SOURCE TEXT so templates stay
readable and the English rendering is byte-identical to a non-localized app:

- ``t(text, **kw)``    — static template/UI strings. Looks *text* up in ``ZH``
  (exact match), falls back to the English text itself. Keyword args are
  ``str.format`` placeholders, so Chinese can reorder them freely instead of
  gluing fragments in English word order.
- ``td(text)``         — dynamic text that was *stored* at run time in English
  (progress messages, parser warnings, phase names). Tries ``ZH`` exactly,
  then the ``ZH_PATTERNS`` regexes (which carry row numbers, counts and other
  captured values into the Chinese sentence), else returns the text as-is —
  an untranslated diagnostic degrades to English, never to a broken string.

The Chinese copy is written for the actual audience — a China-side social /
KOL operations team working with 小红书, DMR exports and Perimeter lists —
not as literal translation: domain vocabulary the team already uses stays
untouched (无博主 / 无帖子 / 人工复核 / 报备 / 软植 / Perimeter / PLOG / DMR
/ CPM / CPE), a reconciliation *run* is 核对, and sentences are rebuilt
around the meaning rather than the English syntax.
"""
from __future__ import annotations

import re
from typing import Callable

from fastapi import Request

SUPPORTED = ("en", "zh")
COOKIE = "dmr_lang"

# --------------------------------------------------------------- static UI
# English source text → Chinese. Populated by every template's strings; the
# tests scan templates for t("...") calls and assert full coverage here.

ZH: dict[str, str] = {
    # base chrome -----------------------------------------------------------
    "PLOG · DMR · Xiaohongshu": "PLOG · DMR · 小红书",
    "Efficiency": "投放效率",
    "Team": "团队",
    "Sign out": "退出登录",

    # auth / server messages (main.py) --------------------------------------
    "Wrong username or password.": "用户名或密码错误。",
    "Wrong setup code.": "设置码错误。",
    "APP_PASSWORD is not configured — authentication is disabled.":
        "服务器未配置 APP_PASSWORD——登录功能未启用。",
    "Username: 2-32 chars, a-z 0-9 . _ - (starts alphanumeric)":
        "用户名：2–32 位，可用 a-z、0-9、.、_、-（须以字母或数字开头）",
    "Password must be at least 8 characters.": "密码至少需要 8 个字符。",
    "Only admins can add accounts.": "只有管理员可以添加账号。",
    "User {username} already exists.": "用户名 {username} 已被占用。",
    "Account {username} created — share the initial password with them privately.":
        "已创建账号 {username}——请通过私下渠道把初始密码告诉对方。",
    "Only admins can remove accounts.": "只有管理员可以移除账号。",
    "No such user.": "没有这个用户。",
    "You cannot delete your own account.": "不能移除自己的账号。",
    "Cannot delete the last admin.": "不能移除最后一名管理员。",
    "Account {username} removed.": "已移除账号 {username}。",
    "Not signed in.": "尚未登录。",
    "Only admins can reset other passwords.": "只有管理员可以重置他人的密码。",
    "Password updated for {username}.": "已更新 {username} 的密码。",
    "Run not found": "找不到这条核对记录",
    "Could not read the uploaded file(s) as .xlsx: {e}":
        "上传的文件无法按 .xlsx 读取：{e}",
    "Could not read the uploaded file as .xlsx: {e}":
        "上传的文件无法按 .xlsx 读取：{e}",
    "Could not read the perimeter file: {e}":
        "Perimeter 文件无法读取：{e}",
    "Internal cross-check failed — report not generated: {e}":
        "内部交叉校验未通过——报告未生成：{e}",
    "This report has expired (reports are kept in memory for 2 hours, never stored). Re-upload the workbook.":
        "该报告已过期（报告只在内存中保留 2 小时，绝不落盘存储）。请重新上传工作簿。",

    # runtime progress / phases (stored in English, translated at render) ---
    "Parsing workbooks…": "正在解析工作簿…",
    "Adjudicating residue with Claude…": "正在让 Claude 复核剩余存疑行…",
    "Drafting run summary…": "正在撰写核对摘要…",
    "Run complete.": "核对完成。",
    "Run interrupted by a restart — use Retry.": "核对因服务重启而中断——请点「重试」。",
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
    # template strings (merged from the per-template translation pass) --
    "(an existing username is reset)": "（若该用户名已存在，会重置其账号）",
    " (detected by Blogger + PostID fingerprint)": "（按 Blogger + PostID 指纹识别）",
    " (detected by NAME + POST LINK fingerprint)": "（按 NAME + POST LINK 指纹识别）",
    "(no API key configured)": "（未配置 API 密钥）",
    "(replaces the current one below)": "（会替换下方当前这份）",
    "(you)": "（本人）",
    "(": "（",
    ")": "）",
    "PLOG file (e.g.": "PLOG 文件（例如",
    "DMR file (e.g.": "DMR 文件（例如",
    "ERROR": "错误",
    "WARN": "警告",
    "<b>Deck not generated</b> — a validation error below is blocking (missing_row_policy=block). Fix the source data or re-run with the default policy.":
        "<b>未生成幻灯片</b>——下方有一条阻断性的校验错误（missing_row_policy=block）。请修正源数据，或改用默认策略重新生成。",
    "ANTHROPIC_API_KEY is not configured — Tier-4 adjudication and the bilingual summary are skipped; ambiguous rows stay 人工复核.":
        "未配置 ANTHROPIC_API_KEY——将跳过 Tier-4 复核和双语摘要；存疑行会停在「人工复核」。",
    "APP_PASSWORD is not configured on the server, so authentication is disabled and no accounts are needed.":
        "服务器未配置 APP_PASSWORD，登录功能未启用，无需创建账号。",
    "APP_PASSWORD is not configured — the app runs open and accounts are disabled.":
        "服务器未配置 APP_PASSWORD——应用无需登录即可访问，账号功能未启用。",
    "Add a coworker": "添加同事",
    "Adjudicator model:": "复核模型：",
    "Adjudicator unsure — treat the candidate list with care.":
        "复核模型没有把握——候选列表请谨慎参考。",
    "Admin": "管理员",
    "Administrator (can manage accounts)": "管理员（可管理账号）",
    "Analyze & generate": "分析并生成",
    "Author id established from another PLOG row of the same blogger (identical NAME) — this row's note detail is dead/blocked, but blogger presence is still decidable.":
        "作者 ID 取自同一博主（NAME 相同）的另一条 PLOG 行——本行笔记详情已失效或被拦截，但仍可判定博主是否在库。",
    "Avg price": "平均合作价",
    "Back": "返回",
    "Back to sign in": "返回登录",
    "CPE per-post": "CPE 单篇平均",
    "CPE pooled": "CPE 合并口径",
    "CPM / CPE basis": "CPM / CPE 口径",
    "CPM per-post": "CPM 单篇平均",
    "CPM pooled": "CPM 合并口径",
    "Campaign": "投放项目",
    "Campaign sections": "投放项目分组",
    "Cancel": "取消",
    "Candidate list is ranked by date proximity only — never auto-matched.":
        "候选列表仅按日期接近度排序——绝不自动判定为匹配。",
    "Candidates": "候选",
    "Change a password": "重置密码",
    "Change your password": "修改密码",
    "Code": "编号",
    "Column S": "S 列",
    "Continue": "继续",
    "Create account": "创建账号",
    "Create admin account": "创建管理员账号",
    "Create the account with an initial password and share it privately; they can change it here afterwards.":
        "先设一个初始密码创建账号，私下把密码告诉对方；对方之后可以在这里自行修改。",
    "DMR gaps（无帖子 + Perimeter内无博主）— actionable":
        "DMR 漏抓（无帖子 + Perimeter内无博主）——可跟进",
    "DMR posts inside the campaign window by bloggers this campaign resolved, whose PostID matched no PLOG row — extra posts DMR captured that PLOG doesn't track.":
        "本次核对已解析出的博主在投放窗口期内发布、但 PostID 没有对应到任何 PLOG 行的 DMR 帖子——也就是 DMR 抓到了、PLOG 却没在跟踪的额外帖子。",
    "DMR row": "DMR 行号",
    "DMR sheet row {n}": "DMR 表第 {n} 行",
    "DMR:": "DMR：",
    "Data rows": "数据行数",
    "Date": "发帖日期",
    "Date range": "日期范围",
    "Date Δ": "日期差",
    "Decided by": "判定 Tier",
    "Display": "显示名",
    "Display name (optional)": "显示名（选填）",
    "Download .pptx": "下载 .pptx",
    "Download annotated .xlsx": "下载标注版 .xlsx",
    "Efficiency report — {name}": "投放效率报告 — {name}",
    "Engagement": "互动数",
    "Evidence": "判定依据",
    "Excel rows": "Excel 行号",
    "Export window": "导出窗口",
    "Files": "文件",
    "Finding": "发现",
    "First-time setup": "首次设置",
    "From FAN BASE thresholds (≥1000K TOP · ≥400K MID · ≥200K BOT · else KOC)":
        "按 FAN BASE 粉丝量阈值（≥1000K TOP · ≥400K MID · ≥200K BOT · 其余 KOC）",
    "From LEVEL labels (头部/腰部/尾部+底部→BOT/KOC)":
        "按 LEVEL 标签（头部/腰部/尾部+底部→BOT/KOC）",
    "Generate →": "生成 →",
    "Group": "分组",
    "Group metrics": "分组指标",
    "Header row": "表头行",
    "Human override": "人工改判",
    "Initial password (min 8 characters)": "初始密码（至少 8 个字符）",
    "Insights (data-driven only)": "洞察（仅基于本次数据）",
    "JSON audit log": "JSON 审计日志",
    "KOL Efficiency Report": "KOL 投放效率报告",
    "KOL efficiency report": "KOL 投放效率报告",
    "KOL workbook (e.g.": "KOL 工作簿（例如",
    "LLM adjudication returned malformed JSON twice — kept for human review.":
        "LLM 复核两次返回的 JSON 都格式不对——该行保留待人工复核。",
    "LLM calls:": "LLM 调用：",
    "Link dead/unresolvable and no name-based candidate either.":
        "链接失效或无法解析，也没有任何按名字匹配到的候选。",
    "Loading…": "加载中…",
    "Matched DMR": "匹配到的 DMR",
    "Matched row": "匹配行",
    "Member": "成员",
    "Message": "进度信息",
    "Missing groups are absent — rendered as gaps on the slide, never zero bars. The source file's CPM column (price per <i>single</i> impression) is never reused; CPM here is the industry-standard ¥ per 1,000.":
        "缺失的分组不会出现——幻灯片上呈现为空档，绝不画成零值柱。源文件的 CPM 列是「<i>单次</i>曝光单价」，从不复用；此处 CPM 采用行业口径：每千次曝光成本（¥/1000）。",
    "Name": "博主名",
    "Name method": "名字匹配方式",
    "Native editable charts — every cached chart value is verified against the computed metrics before the file is offered.":
        "原生可编辑图表——文件提供下载前，图表内缓存的每个数值都已与计算指标核验一致。",
    "New password (min 8 characters)": "新密码（至少 8 个字符）",
    "New reconciliation run": "新建核对",
    "New report": "再生成一份",
    "No accounts exist yet — <a href=\"/setup\" style=\"color:var(--ink)\">create the admin account</a> with the setup code.":
        "还没有任何账号——请用设置码<a href=\"/setup\" style=\"color:var(--ink)\">创建管理员账号</a>。",
    "No perimeter uploaded — 无博主 rows are not split by perimeter membership.":
        "尚未上传 Perimeter 名单——「无博主」的行不会按名单内外拆分。",
    "No perimeter — 无博主 rows stay unsplit.": "未上传 Perimeter——「无博主」行不做拆分。",
    "No runs yet.": "还没有核对记录。",
    "No wave-over-wave comparisons are generated — this report sees one wave only. This page and the download expire two hours after generation; nothing is stored server-side.":
        "不生成波次间对比——本报告只看得到当前这一个波次的数据。页面和下载文件在生成 2 小时后过期；服务器端不存储任何内容。",
    "Note": "备注",
    "Nothing found.": "没有发现额外帖子。",
    "Outside DMR export window": "超出 DMR 导出窗口",
    "PLOG LIKE {a} vs DMR Likes_Retweet {b}":
        "PLOG LIKE {a}，DMR Likes_Retweet {b}",
    "PLOG:": "PLOG：",
    "Parse preview — confirm before running": "解析预览——确认后开始核对",
    "Password": "密码",
    "Password (min 8 characters)": "密码（至少 8 个字符）",
    "Past runs": "历史核对",
    "Per-post average — mean of each post's own ratio": "单篇平均——每篇帖子各自比值的平均",
    "Perimeter (Micro) — optional": "Perimeter（Micro）名单——选填",
    "Perimeter (Micro):": "Perimeter（Micro）：",
    "Perimeter in use:": "当前使用的 Perimeter：",
    "Perimeter:": "Perimeter：",
    "Pooled — group Σspend ÷ Σimpressions (campaign-level truth)":
        "合并口径——组内 Σ花费 ÷ Σ曝光（投放整体的真实成本）",
    "Remove": "移除",
    "Remove account {username}?": "确定要移除账号 {username} 吗？",
    "Remove the perimeter? Runs revert to plain 无博主.":
        "移除 Perimeter 名单？之后的核对将退回普通的「无博主」。",
    "Reset admin account": "重置管理员账号",
    "Resolution": "解析来源",
    "Resolved author": "解析出的作者",
    "Resolved note": "解析出的笔记",
    "Results ({n})": "核对结果（{n}）",
    "Retry links whose previous resolution failed (successful resolutions are always served from cache and never re-fetched)":
        "重试之前解析失败的链接（解析成功的链接一律走缓存，不会重新抓取）",
    "Retry run": "重试",
    "Reverse audit ({n})": "反向核查（{n}）",
    "Role": "角色",
    "Rows": "行数",
    "Run": "核对",
    "Run failed": "核对失败",
    "Run reconciliation": "开始核对",
    "Run {id}": "核对 {id}",
    "Setup code": "设置码",
    "Severity": "级别",
    "Share": "占比",
    "Sheet": "工作表",
    "Sign in": "登录",
    "Slide language": "幻灯片语言",
    "Something went wrong": "出错了",
    "Status": "状态",
    "TIKHUB_API_KEY is not configured — link resolution will rely on the free direct-redirect path only, which is usually blocked from datacenter IPs. Expect most rows to fall back to Check链接错误 with name-based candidates.":
        "未配置 TIKHUB_API_KEY——解析链接只能走免费的直接重定向通道，而机房 IP 通常会被拦。预计大多数行会退回「Check链接错误」，并给出按名字匹配的候选。",
    "Team — DMR Reconciler": "团队 — DMR Reconciler",
    "The setup code is the server's": "设置码就是服务器的",
    "The source file is never mutated — issues are reported here and, where they bias a metric, called out on the slide itself.":
        "绝不改动源文件——问题只在此处列出；若会使某项指标失真，也会直接在对应幻灯片上注明。",
    "This run has not been started.": "这条核对还没有开始。",
    "Tier assignment": "层级判定",
    "TikHub calls this run:": "本次核对 TikHub 调用：",
    "TikHub calls:": "TikHub 调用：",
    "Top-2 impression share": "前两篇曝光占比",
    "Turn a PLOG tracker into a chart-based efficiency presentation (TIER × 报备/软植 CPM & CPE, price premiums, validation caveats) — editable":
        "把 PLOG 追踪表做成一份图表化的投放效率汇报（TIER × 报备/软植 的 CPM 与 CPE、价格溢价、校验提示）——可编辑的",
    "Update password": "更新密码",
    "Upload & preview": "上传并预览",
    "Upload a PLOG-style tracker (": "上传 PLOG 格式的追踪表（需包含 ",
    "Upload again": "重新上传",
    "Upload the internal tracker (PLOG) and the DMR social-listening export. You will see a parse preview before anything runs — no TikHub or Claude calls happen until you confirm.":
        "上传内部追踪表（PLOG）和 DMR 社媒监测导出文件。开始前会先显示解析预览——在确认之前不会调用 TikHub 或 Claude。",
    "Use Claude for residue adjudication + bilingual summary":
        "用 Claude 复核剩余存疑行并生成双语摘要",
    "Username": "用户名",
    "Validation findings ({n})": "数据校验发现（{n}）",
    "basis": "指标按",
    "counted in totals, excluded from group metrics (V7)": "计入总量，不计入分组指标（V7）",
    "download and an HTML view. Nothing is stored: the workbook is analyzed in memory and the report expires after two hours.":
        "下载，以及 HTML 网页版。任何数据都不落盘：工作簿只在内存中分析，报告两小时后自动过期。",
    "engagements": "总互动",
    "excluded (V2)": "条因 V2 排除",
    "extracted {date}": "提取于 {date}",
    "extraction: {date}": "提取日期：{date}",
    "fanbase": "FAN BASE 阈值",
    "impressions": "总曝光",
    "label": "LEVEL 标签",
    "likes": "点赞",
    "matched via {method}:": "匹配方式 {method}：",
    "override": "人工改判",
    "pending": "待开始",
    "per_post": "单篇平均",
    "plus an HTML view. Runs entirely in memory; nothing is stored.":
        "文件，外加 HTML 网页版。全程只在内存中运行，不保存任何数据。",
    "pooled": "合并口径",
    "posts": "篇帖子",
    "queued": "排队中",
    "running": "进行中",
    "secret. The account created here becomes an administrator":
        "密钥。在这里创建的账号将成为管理员",
    "sheet": "工作表",
    "sheet). The analyzer classifies every post into TIER × 报备/软植 groups, validates the data (duplicate links, engagement identities, missing values, small samples, viral-post concentration), and produces a chart-based one-slide presentation — as an editable":
        "工作表）。分析器会把每篇帖子归入 TIER × 报备/软植 分组，先做数据校验（重复链接、互动量勾稽、缺失值、样本量过小、爆款帖集中度），再生成一页式图表汇报——提供可编辑的",
    "spend": "总花费",
    "tier {n}": "Tier {n}",
    "tiers from": "层级取自",
    "unclassified": "条未分类",
    "unknown date": "日期未知",
    "{filename} · extracted {date} · {rows} rows, {redbook} with REDBOOK_ID — 无博主 rows will be split by membership.":
        "{filename} · 提取于 {date} · 共 {rows} 行，其中 {redbook} 行有 REDBOOK_ID——「无博主」行将按是否在名单内拆分。",
    "{filename} · extracted {date} · {rows} rows, {redbook} with REDBOOK_ID.":
        "{filename} · 提取于 {date} · 共 {rows} 行，其中 {redbook} 行有 REDBOOK_ID。",
    "{n} PLOG row(s) have a POST DATE outside the DMR export window — an absent post there is expected-missing, not a DMR gap. They are flagged in the results.":
        "有 {n} 行 PLOG 的 POST DATE 在 DMR 导出窗口之外——这些行查不到帖子属于预期缺失，而不是 DMR 漏抓，结果中已相应标记。",
    "{n} candidate(s)": "{n} 个候选",
    "{n} days": "{n} 天",
    "{n} followers": "{n} 粉丝",
    "{n} rows parsed": "已解析 {n} 行",
    "— keep the code private.": "——设置码务必保密。",
    "— pipeline verdict —": "—— 系统自动判定 ——",
    "不在Perimeter内 — out of scope": "不在Perimeter内——无需处理",
    "在Perimeter名单但未登记REDBOOK_ID — register the ID; DMR cannot crawl an unregistered account":
        "在Perimeter名单但未登记REDBOOK_ID——请先登记该 ID；未登记的账号 DMR 抓不到。",
    "无博主 rows will be split by perimeter membership. The perimeter updates regularly — replace it when the extraction date gets stale.":
        "「无博主」的行会按是否在 Perimeter 名单内拆分。Perimeter 名单会定期更新——提取日期旧了就换一份新的。",
    "链接失效，仅作参考：名字命中Perimeter条目 / dead link — perimeter name hits recorded as evidence only, verdict unchanged":
        "链接失效，仅作参考：名字命中Perimeter条目——仅记录为判定依据，结论不变。",
}

# ------------------------------------------------- dynamic (parameterized)
# Stored English diagnostics that embed row numbers / counts / cell values.
# Each pattern must consume the whole message; captured groups are spliced
# into the Chinese sentence where *its* grammar wants them.

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
    (re.compile(r"^V1: no header row containing NAME and POST LINK found in "
                r"sheet (.+)\.$"),
     r"V1：工作表 \1 中找不到包含「NAME」和「POST LINK」的表头行。"),
    (re.compile(r"^V1: required columns missing after header normalization: (.+)$",
                re.S),
     r"V1：表头标准化后仍缺少必需列：\1"),
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


def get_lang(request: Request) -> str:
    lang = request.cookies.get(COOKIE, "en")
    return lang if lang in SUPPORTED else "en"


def make_t(lang: str) -> Callable[..., str]:
    def t(text: str, **kw) -> str:
        s = ZH.get(text, text) if lang == "zh" else text
        return s.format(**kw) if kw else s
    return t


def make_td(lang: str) -> Callable[[str], str]:
    def td(text: str) -> str:
        if lang != "zh" or not text:
            return text
        hit = ZH.get(text)
        if hit is not None:
            return hit
        for pat, repl in ZH_PATTERNS:
            if pat.match(text):
                return pat.sub(repl, text)
        return text
    return td


def context(request: Request) -> dict:
    """Jinja context processor — every template gets lang / t / td."""
    lang = get_lang(request)
    return {"lang": lang, "t": make_t(lang), "td": make_td(lang)}
