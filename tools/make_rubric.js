const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  WidthType, ShadingType, HeadingLevel, AlignmentType, BorderStyle,
  LevelFormat, PageBreak,
} = require("docx");
const fs = require("fs");
const path = require("path");

const INK = "111111";
const MUTED = "6b6b6b";
const LINE = "d9d6d0";
const WASH = "f5f4f1";
const BODY_FONT = {
  ascii: "Calibri",
  hAnsi: "Calibri",
  eastAsia: "Arial Unicode MS",
  cs: "Calibri",
};

const bullets = {
  config: [{
    reference: "b",
    levels: [{
      level: 0, format: LevelFormat.BULLET, text: "•",
      style: { paragraph: { indent: { left: 340, hanging: 180 } } },
    }],
  }],
};

const en = (t, opts = {}) => new TextRun({
  text: t, size: 20, color: INK, font: BODY_FONT, ...opts,
});
const zh = (t, opts = {}) => new TextRun({
  text: t, size: 20, color: MUTED, font: BODY_FONT, ...opts,
});

function para(children, opts = {}) { return new Paragraph({ children, spacing: { after: 80 }, ...opts }); }
function bullet(enText, zhText) {
  return new Paragraph({
    numbering: { reference: "b", level: 0 }, spacing: { after: 60 },
    children: [en(enText), zh("  " + zhText)],
  });
}
function h1(t) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1, spacing: { before: 280, after: 120 },
    children: [new TextRun({ text: t, size: 30, bold: true, color: INK })],
  });
}
function h2(t) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2, spacing: { before: 220, after: 100 },
    children: [new TextRun({ text: t, size: 24, bold: true, color: INK })],
  });
}

const CELL_MARGIN = { top: 60, bottom: 60, left: 100, right: 100 };
const BORDER = { style: BorderStyle.SINGLE, size: 4, color: LINE };
const BORDERS = { top: BORDER, bottom: BORDER, left: BORDER, right: BORDER };

function cell(width, paras, opts = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA }, margins: CELL_MARGIN,
    borders: BORDERS, ...opts, children: paras,
  });
}
function headCell(width, text) {
  return cell(width, [new Paragraph({
    children: [new TextRun({ text, size: 18, bold: true, color: INK })],
  })], { shading: { type: ShadingType.CLEAR, fill: WASH } });
}

// column-spec table: Column | Required | Rule (EN over ZH)
function specTable(rows) {
  const W = [1750, 1500, 6110];
  return new Table({
    columnWidths: W,
    width: { size: 9360, type: WidthType.DXA },
    rows: [
      new TableRow({ children: [headCell(W[0], "Column 列名"), headCell(W[1], "Required 必填"), headCell(W[2], "Rule 规则")] }),
      ...rows.map(([col, req, enR, zhR]) => new TableRow({
        children: [
          cell(W[0], [para([new TextRun({ text: col, size: 19, bold: true, font: "Courier New" })])]),
          cell(W[1], [para([en(req)])]),
          cell(W[2], [para([en(enR)], { spacing: { after: 30 } }), para([zh(zhR)], { spacing: { after: 0 } })]),
        ],
      })),
    ],
  });
}

const doc = new Document({
  numbering: bullets,
  styles: { default: { document: { run: { font: BODY_FONT, size: 20 } } } },
  sections: [{
    properties: { page: { margin: { top: 1080, bottom: 1080, left: 1080, right: 1080 } } },
    children: [
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 40 },
        children: [new TextRun({ text: "DMR RECONCILER", size: 36, bold: true, color: INK, characterSpacing: 60 })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 60 },
        children: [new TextRun({ text: "Input File Formatting Rubric · 输入文件格式规范", size: 24, color: INK })],
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER, spacing: { after: 240 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: INK, space: 6 } },
        children: [zh("Give this to whoever prepares the PLOG tracker or the DMR export. 请把本规范交给制作 PLOG 追踪表或 DMR 导出文件的同事/供应商。", { size: 18 })],
      }),

      h1("0. Golden rules · 通用规则"),
      bullet("Excel .xlsx only, maximum 25 MB per file. No .xls, .csv, or password protection.",
             "只接受 Excel .xlsx，单个文件不超过 25 MB。不要用 .xls、.csv，不要加密码。"),
      bullet("Keep the column headers EXACTLY as listed below. Case and spacing are forgiven, but renamed columns are not.",
             "表头名称必须与下文完全一致。大小写和空格无所谓，但改名不行。"),
      bullet("The header row may sit anywhere in the first 15 rows — notes above it are fine. One header row, one table per sheet.",
             "表头行可在前 15 行内的任意位置——上方可以有说明行。每张工作表只放一个表头、一张表。"),
      bullet("Never merge two records into one row, and never split one record across two rows.",
             "不要把两条记录并进一行，也不要把一条记录拆成两行。"),
      bullet("A different layout enters manual column mapping only when the uploader explicitly consents, Claude is configured, and an administrator approves the proposal; otherwise it is rejected. Following this rubric skips that flow.",
             "格式不同时，只有上传者明确同意、系统已配置 Claude、且管理员审核通过建议后，才会进入人工列映射流程；否则文件会被拒绝。按本规范来可跳过该流程。"),

      h1("1. PLOG tracker · PLOG 投放追踪表"),
      para([en("Sheet name: ", { bold: true }), new TextRun({ text: "MASTER KOL LIST", size: 20, font: "Courier New" }),
            en(".  One row = one post. Header row (copy it exactly):"),
            zh("  工作表名 MASTER KOL LIST。一行 = 一篇帖子。表头如下（原样照抄）：")]),
      para([new TextRun({
        text: "NO | MCN | CAMPAIGN | TYPE | LEVEL | NAME | FAN BASE（K) | POST DATE | MICRO MACRO | POST LINK | IMPRESSION | LIKE | COLLECTION | COMMENT | TTL ENGAGEMENT | PRICE | CPM | CPE",
        size: 17, font: "Courier New", color: INK,
      })], { shading: { type: ShadingType.CLEAR, fill: WASH }, spacing: { after: 160 } }),

      specTable([
        ["NAME", "YES 必填",
         "The KOL's Xiaohongshu display name, exactly as shown on their profile. Emoji are fine.",
         "博主在小红书上的昵称，与主页显示完全一致。可以带 emoji。"],
        ["POST LINK", "YES 必填",
         "The post's link: an xhslink.com share link or a xiaohongshu.com note URL. Plain text or a hyperlink cell — both work. ONE link per row, and never the same link on two rows.",
         "帖子链接：xhslink.com 分享短链或 xiaohongshu.com 笔记链接。纯文字或超链接单元格都行。一行一条链接，绝不能两行共用同一条。"],
        ["TYPE", "YES 必填",
         "Must begin with 报备 (declared/paid) or 软植 (soft placement) — e.g. 报备图文, 软植图文, 报备视频.",
         "必须以「报备」或「软植」开头——如 报备图文、软植图文、报备视频。"],
        ["LEVEL", "YES 必填",
         "One of: 头部 / 腰部 / 尾部 / 底部 / KOC. Blank, 待定, and ? fall back to FAN BASE (≥1000K TOP · ≥400K MID · ≥200K BOT · otherwise KOC) and are flagged as V12. Any other label stays unclassified.",
         "只能填：头部 / 腰部 / 尾部 / 底部 / KOC。留空、待定或 ? 时按粉丝量回退分层（≥1000K TOP · ≥400K MID · ≥200K BOT · 其余 KOC），并以 V12 提醒；其他标签保持未分类。"],
        ["FAN BASE（K)", "YES 必填",
         "Use ONE unit for the entire workbook and choose the same unit on the report form. Recommended: thousands (130 means 130,000). Raw mode is also supported (130000 means 130,000). Never mix units row by row.",
         "整份工作簿只能使用一种单位，并在报告表单中选择相同单位。建议用「千」（130 表示 13 万）；也支持原始粉丝数模式（130000 表示 13 万）。绝不能逐行混用单位。"],
        ["POST DATE", "YES 必填",
         "The date the post went live, as a real Excel date cell (preferred) or text like 2026-06-25. Four-digit years, please. Never a bare number.",
         "发帖日期。最好用 Excel 日期格式，文字形式请写 2026-06-25。年份写四位。不要填纯数字。"],
        ["IMPRESSION / LIKE / COLLECTION / COMMENT", "YES 必填",
         "Plain whole numbers (commas OK). No text like “1.2w”.",
         "纯整数（可带千分位逗号）。不要写「1.2w」这类文字。"],
        ["TTL ENGAGEMENT", "YES 必填",
         "Must equal LIKE + COLLECTION + COMMENT exactly — the tool checks this identity on every row.",
         "必须严格等于 点赞+收藏+评论 之和——工具会逐行校验。"],
        ["PRICE", "YES 必填",
         "Collaboration price in CNY, number only (23000, not ¥23,000元).",
         "合作价格（人民币），只填数字（23000，不要写 ¥ 或 元）。"],
        ["NO / CAMPAIGN / MCN", "Suggested 建议",
         "NO restarts from 1 within each campaign. CAMPAIGN may be written once at the top of its section — rows below inherit it.",
         "NO 在每个 campaign 内从 1 重新编号。CAMPAIGN 可只在段落首行填写，下面的行自动沿用。"],
        ["CPM / CPE", "Optional 选填",
         "The tool never uses these — it recomputes both from PRICE and IMPRESSION/ENGAGEMENT. (Note: this file's CPM is price per single impression, ×1000 off the industry standard — another reason it is never reused.)",
         "工具不使用这两列——CPM/CPE 会用 PRICE 和曝光/互动重新计算。（此文件的 CPM 是单次曝光单价，与行业口径差 1000 倍，因此绝不复用。）"],
        ["Column S and beyond S 列及以后", "Leave EMPTY 请留空",
         "The tool writes its verdicts in column S and its evidence in the columns after it. Anything you leave there is preserved, not overwritten — but a clean file gives the clearest output.",
         "工具会把判定结果写入 S 列、判定依据写入其后各列。你留下的内容会被保留、不会被覆盖——但留空能得到最干净的输出。"],
      ]),

      h1("2. DMR export · DMR 导出文件"),
      para([en("Any sheet name (e.g. "), new TextRun({ text: "Streaming", size: 20, font: "Courier New" }),
            en(").  Keep the generation-info cell above the header — its “From … To …” line defines the export window:"),
            zh("  工作表名不限（如 Streaming）。表头上方的生成信息必须保留——其中「From … To …」定义导出窗口：")]),
      para([new TextRun({
        text: "User: …  Generation date: 20/07/2026 …  Top Bloggers - From 01/01/2026 To 20/07/2026",
        size: 17, font: "Courier New", color: INK,
      })], { shading: { type: ShadingType.CLEAR, fill: WASH }, spacing: { after: 160 } }),

      specTable([
        ["PostID", "YES 必填",
         "The Xiaohongshu note id: exactly 24 hexadecimal characters (e.g. 69674d92000000000c0364…). This is THE join key. Format the column as Text so Excel cannot mangle it into scientific notation.",
         "小红书笔记 ID：24 位十六进制字符。这是最关键的关联键。请把整列设为「文本」格式，防止 Excel 转成科学计数法。"],
        ["Username", "YES 必填",
         "The author's platform user id (24-hex). Without this column the tool cannot distinguish 无博主 from 无帖子 — do not omit or blank it.",
         "作者的平台用户 ID（24 位十六进制）。没有这一列就无法区分「无博主」和「无帖子」——不能省略、不能留空。"],
        ["Blogger", "YES 必填",
         "The blogger's display name as crawled.",
         "抓取到的博主昵称。"],
        ["PostDate", "YES 必填",
         "Crawl-recorded post date/time.",
         "抓取记录的发帖时间。"],
        ["Likes_Retweet / Share_Favorites / Engagement / Comments", "YES 必填",
         "First-crawl engagement snapshot, whole numbers. (These are context only — the tool never uses engagement to decide a match.)",
         "首次抓取的互动快照，整数。（仅作参考——工具绝不用互动数判断匹配。）"],
        ["Link", "Suggested 建议",
         "The post URL; a hyperlink whose target embeds the note id is ideal (used as a cross-check against PostID).",
         "帖子链接；最好是内嵌笔记 ID 的超链接（用于和 PostID 交叉校验）。"],
        ["Export window 导出窗口", "YES 必须",
         "The From/To dates must cover every campaign post date, or posts outside them are treated as expected-missing rather than checked. (The window can be edited in-app, but an accurate export beats a patched one.)",
         "From/To 日期必须覆盖所有投放帖子的发帖日期，否则窗口外的帖子按「预期缺失」处理、不参与核对。（窗口可在系统内修改，但导出准确永远优于事后修补。）"],
      ]),

      h1("3. Perimeter workbook (optional) · Perimeter 名单（选交）"),
      bullet("The LVMH workbook containing a “List Micro” sheet, with NAME and REDBOOK_ID columns and the “Date of extraction :” line above the header.",
             "含「List Micro」工作表的 LVMH 名单文件，需有 NAME 和 REDBOOK_ID 列，表头上方保留「Date of extraction :」行。"),
      bullet("Only China-market rows are used (IN_CHINA_REPORTS = YES, or COUNTRY = Mainland China). Send the freshest extraction — the date is shown in-app.",
             "只使用中国市场的行（IN_CHINA_REPORTS=YES，或 COUNTRY=Mainland China）。请交最新提取的版本——提取日期会在系统中显示。"),

      h1("4. Pre-send checklist · 交付前自查清单"),
      bullet("File is .xlsx, under 25 MB, not password-protected.", "文件为 .xlsx，小于 25 MB，未加密。"),
      bullet("Headers match this rubric word-for-word.", "表头与本规范逐字一致。"),
      bullet("PLOG: every row has NAME + POST LINK; no two rows share a link.", "PLOG：每行都有 NAME 和 POST LINK；没有两行共用同一链接。"),
      bullet("PLOG: TTL ENGAGEMENT = LIKE + COLLECTION + COMMENT on every row.", "PLOG：每行 TTL ENGAGEMENT 等于三项互动之和。"),
      bullet("PLOG: FAN BASE uses one declared workbook unit; TYPE starts with 报备/软植; LEVEL uses the five allowed labels or an approved fallback placeholder.", "PLOG：整份表的粉丝量使用同一个已声明单位；TYPE 以报备/软植开头；LEVEL 只用五个规定标签或允许回退的占位值。"),
      bullet("DMR: PostID column formatted as Text, 24-hex values intact; Username column present and filled.", "DMR：PostID 列为文本格式、24 位十六进制完整；Username 列存在且有值。"),
      bullet("DMR: the From/To window line is present and covers the campaign dates.", "DMR：From/To 窗口行存在，且覆盖投放日期范围。"),

      new Paragraph({
        spacing: { before: 300 },
        border: { top: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 6 } },
        children: [zh("Questions or a source system that cannot match this layout? Ask an administrator whether the consent-gated mapping flow is configured; the file is rejected unless the uploader opts in, Claude is available, and an administrator approves the proposal. Tell the team so this rubric can be extended.  如有疑问，或源系统实在无法输出此格式：请先让管理员确认受同意机制保护的列映射流程已配置；只有上传者明确同意、Claude 可用且管理员审核通过建议后，文件才会被接受。也请告知团队，以便扩充本规范。", { size: 17 })],
      }),
    ],
  }],
});

const outputPath = path.resolve(
  process.argv[2] || path.join(__dirname, "..", "docs", "DMR_Reconciler_File_Rubric.docx"),
);
fs.mkdirSync(path.dirname(outputPath), { recursive: true });
Packer.toBuffer(doc)
  .then((buf) => {
    fs.writeFileSync(outputPath, buf);
    console.log(`written to ${outputPath}`);
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
