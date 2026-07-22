"""Shared UI strings: chrome, auth, first-visit guide, header-remap audit,
and the per-template static strings of both products.
Merged into the app-wide catalog by app.i18n — see that module for the
translation contract (English-source keys, whole-message patterns).
"""
from __future__ import annotations

import re  # noqa: F401  (patterns)

ZH: dict[str, str] = {
    # base chrome -----------------------------------------------------------
    "PLOG · DMR · Xiaohongshu": "PLOG · DMR · 小红书",
    "Efficiency": "投放效率",
    "Team": "团队",
    "Sign out": "退出登录",

    # auth / server messages (main.py) --------------------------------------
    "Wrong username or password.": "用户名或密码错误。",
    "Too many failed attempts — wait {s} seconds and try again.":
        "失败次数过多——请等待 {s} 秒后再试。",
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

    # first-visit guide (base.html modal) -----------------------------------
    "Guide": "使用指南",
    "Quick guide": "使用指南",
    "Close guide": "关闭指南",
    "Got it": "知道了",
    "What this app does": "这个工具是做什么的",
    "It reconciles your internal KOL tracker (PLOG) against the DMR social-listening export for Xiaohongshu posts, row by row, and produces the annotated Excel your team otherwise fills in by hand — column S verdicts with the evidence behind each one.":
        "把内部 KOL 追踪表（PLOG）与 DMR 社媒监测导出的小红书帖子逐行核对，自动产出原本要靠人工整理的标注版 Excel——S 列判定结果，以及每条判定背后的判定依据。",
    "Running a reconciliation": "如何发起核对",
    "Efficiency report": "投放效率报告",
    "Upload the PLOG and DMR files on the home page. The Perimeter list is optional — with it, 无博主 rows are split by list membership. Only its China-market rows are used (IN_CHINA_REPORTS=YES, or COUNTRY=Mainland China when that column is absent) — this tool evaluates the Chinese market only.":
        "在首页上传 PLOG 和 DMR 文件。Perimeter 名单选填——上传后，「无博主」的行会按名单内外拆分。名单只取中国市场的行（IN_CHINA_REPORTS=YES；没有该列时取 COUNTRY=Mainland China）——本工具只评估中国市场。",
    "Unfamiliar sheet formats": "表格格式不认识怎么办",
    "If a workbook's headers don't match the known format (say, a tracker with Chinese headers), Claude proposes which column is which — and you review that mapping on an audit screen before anything runs. Each field shows the original header, example values, and a confidence score; correct or reject anything. Only header names get rewritten, never data, and each format needs approving once.":
        "如果工作簿的表头不属于已知格式（比如用中文表头的追踪表），Claude 会先给出「哪列对应哪个字段」的映射建议——在任何处理开始之前，由你在审核页面把关。每个字段都显示原表头、示例值和置信度；可以逐项修改或整体拒绝。应用时只改写表头名称，绝不动数据，而且每种格式只需审核一次。",
    "Check the parse preview (sheets, row counts, date window) and confirm — nothing external is called until you do.":
        "先检查解析预览（工作表、行数、日期窗口）再确认——确认之前不会调用任何外部服务。",
    "The run resolves every post link, then matches by note ID and author ID; only leftover ambiguous rows go to Claude for annotation, plus one final call that drafts the run summary — Claude never decides a verdict.":
        "核对会先解析每条帖子链接，再按笔记 ID 和作者 ID 精确匹配；剩余的存疑行才交给 Claude 补充说明，另有最后一次调用起草核对摘要——Claude 从不做判定。",
    "Review the results — each verdict shows its evidence, any row can be overridden by hand — then download the annotated .xlsx or the JSON audit log.":
        "查看结果——每条判定都附判定依据，任何一行都可以人工改判——最后下载标注版 .xlsx 或 JSON 审计日志。",
    "Reading the verdicts": "判定结果怎么读",
    "The DMR export contains this exact post — column S stays blank. If DMR records the blogger under a wrong name, S reads 有 但是DMR博主名字标注错误 instead.":
        "DMR 导出里有这条帖子——S 列留空；若 DMR 里博主名字标注有误，S 列会改为「有 但是DMR博主名字标注错误」。",
    "The blogger is tracked in DMR but this post is missing — a genuine DMR gap (rows marked 超出DMR导出窗口 are expected-missing, not gaps).":
        "DMR 里有这位博主，但没有这条帖子——真正的 DMR 漏抓（标注「超出DMR导出窗口」的行属预期缺失，不算漏抓）。",
    "The blogger does not appear in the DMR export at all (with a Perimeter list this row is further split by list membership).":
        "DMR 导出里完全没有这位博主（传了 Perimeter 名单时会按名单内外进一步拆分）。",
    "The post link is dead or unresolvable — same-name candidates are listed for review, never auto-matched.":
        "帖子链接失效或无法解析——列出同名候选供人工查看，绝不自动判定为匹配。",
    "Signals conflict or the deciding data is unavailable — a human decides; the reason is shown on the row.":
        "各项信号相互矛盾，或缺少判定所需的数据——需要人工判断；具体原因标注在该行。",
    "Engagement numbers never decide a match — DMR records an early snapshot that is not comparable to PLOG finals.":
        "互动数据从不用于判断匹配——DMR 记录的是发帖早期的快照，与 PLOG 的最终数据不可比。",
    "The Efficiency page turns a PLOG tracker on its own into a one-slide CPM/CPE chart presentation — editable .pptx plus a web view — with data-validation findings shown alongside. It runs entirely in memory and every report expires after two hours.":
        "「投放效率」页面只需一份 PLOG 追踪表，就能生成一页式 CPM/CPE 图表汇报——可编辑 .pptx 外加网页版——同时列出数据校验发现。全程只在内存中运行，每份报告 2 小时后过期。",
    "Switch the interface language any time with the toggle in the top-left corner — this guide reopens from the button next to it.":
        "界面语言随时可以用左上角的按钮切换——本指南也可以从旁边的按钮再次打开。",
    # LLM header mapping + human audit -------------------------------------
    "Header mapping — audit": "表头映射——人工审核",
    "Unfamiliar headers — review the proposed mapping":
        "表头不属于已知格式——请审核建议的映射",
    "This file's headers don't match the format the pipeline knows. Claude looked at the sheet names and the first rows only, and proposed the mapping below — it maps columns, it never touches your data.":
        "该文件的表头与流水线已知的格式不匹配。Claude 只看了工作表名和前几行，给出下面的映射建议——它只负责列的对应，绝不改动你的数据。",
    "Nothing runs until you approve.": "你批准之前不会运行任何处理。",
    "Applying rewrites only the header cells to the canonical names; every data cell stays byte-identical, and the deterministic pipeline runs unchanged. Approved mappings are remembered, so this format is audited once.":
        "应用映射只是把表头单元格改写为标准名称；所有数据单元格保持原样，确定性流水线照常运行。批准过的映射会被记住——同一格式只需审核一次。",
    "header row": "表头行",
    "Model warning:": "模型提示：",
    "Canonical field": "标准字段",
    "Meaning": "含义",
    "Source column": "来源列",
    "Confidence": "置信度",
    "required": "必填",
    "— not present —": "——不存在——",
    "no header": "无表头",
    "e.g.": "如",
    "check this one": "请核对",
    "Approve mapping & continue": "批准映射并继续",
    "Each dropdown shows the column letter, its original header, and example values from that column — correct anything the model got wrong before approving.":
        "每个下拉框都显示列号、原表头和该列的示例值——批准前请把模型映射错的地方改过来。",
    "Headers remapped": "表头已重映射",
    "sheet": "工作表",
    "column {n}": "第 {n} 列",
    "Applied automatically — this exact format was approved by":
        "已自动应用——该格式此前已获批准，批准人：",
    "Approved just now by": "刚刚批准，批准人：",
    "Only header names were rewritten; data cells are untouched.":
        "只改写了表头名称；数据单元格未做任何改动。",
    "Header mapping also failed: {e}": "表头映射也失败了：{e}",
    "Too many mapping audits are active. Try again shortly.":
        "当前正在处理的表头映射审核过多，请稍后重试。",
    "This mapping session has expired — upload the file again.":
        "该映射会话已过期——请重新上传文件。",
    "Required field {field} has no column selected.":
        "必填字段 {field} 尚未选择来源列。",
    "Two fields point at the same column — each column can serve only one field.":
        "有两个字段指向同一列——每列只能对应一个字段。",
    # canonical-field descriptions (schema_map.FIELDS, shown on the audit page)
    "KOL / blogger display name": "KOL／博主昵称",
    "URL of the Xiaohongshu post (often an xhslink.com short link)":
        "小红书帖子链接（常为 xhslink.com 短链）",
    "campaign / wave the row belongs to": "该行所属的投放项目／波次",
    "row number within the campaign": "项目内的行号",
    "date the post went live": "发帖日期",
    "like count": "点赞数",
    "collect/save count": "收藏数",
    "comment count": "评论数",
    "impression / view count": "曝光／浏览量",
    "total engagement (like + collection + comment)":
        "互动总量（点赞＋收藏＋评论）",
    "blogger display name": "博主昵称",
    "Xiaohongshu note id — 24-char hex string":
        "小红书笔记 ID——24 位十六进制字符串",
    "platform author/user id (join key for blogger presence)":
        "平台作者／用户 ID（判断博主是否在库的关联键）",
    "crawl-recorded post date": "抓取记录的发帖日期",
    "likes at first crawl": "首次抓取时的点赞数",
    "shares/favorites": "分享／收藏",
    "total engagement at crawl": "抓取时的互动总量",
    "comment count at crawl": "抓取时的评论数",
    "post URL (hyperlink cell often embeds the note id)":
        "帖子链接（超链接单元格常内嵌笔记 ID）",
    "row number": "行号",
    "campaign / wave name": "投放项目／波次名称",
    "cooperation type — 报备 (declared/paid) vs 软植 (soft placement)":
        "合作类型——报备 vs 软植",
    "tier label — 头部/腰部/尾部/底部/KOC": "层级标签——头部/腰部/尾部/底部/KOC",
    "follower count in thousands": "粉丝量（千）",
    "URL of the post": "帖子链接",
    "collaboration price in CNY": "合作价格（人民币）",
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
    "China market only ({signal}): kept {kept} of {scanned} rows.":
        "仅限中国市场（按 {signal} 判定）：{scanned} 行中保留 {kept} 行。",
    "No IN_CHINA_REPORTS or COUNTRY column found — cannot restrict the perimeter to the China market; keeping all rows.":
        "找不到 IN_CHINA_REPORTS 或 COUNTRY 列——无法把 Perimeter 限定到中国市场；已保留全部行。",
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

ZH_PATTERNS: list[tuple[re.Pattern[str], str]] = [

]
