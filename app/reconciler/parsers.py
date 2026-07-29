"""Schema-tolerant parsing of the PLOG tracker and the DMR export.

Header rows are located by fingerprint (PLOG: a row containing both NAME and
POST LINK; DMR: a row containing both Blogger and PostID), never by fixed
index. Header names are matched after NFKC + whitespace-collapse + casefold,
so the observed quirks (full-width paren in ``FAN BASE（K)``, double space in
``TTL  ENGAGEMENT``) and future cosmetic drift both map cleanly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from openpyxl import load_workbook

from ..core.xlsx import (HEADER_SCAN_ROWS, MAX_CONSECUTIVE_BLANK_ROWS,
                         cell_str, extract_link_target, find_header_row,
                         to_date, to_datetime, to_float, to_int)
from .domain import HEX24, is_hex24


# ---------------------------------------------------------------------- PLOG

@dataclass
class PlogRow:
    campaign: str
    no: str
    name: str
    post_date: Optional[date]
    post_link: str
    like: Optional[int]
    collection: Optional[int]
    comment: Optional[int]
    impression: Optional[int]
    ttl_engagement: Optional[int]
    excel_row: int  # 1-based row in the source sheet (for annotated export)

    @property
    def key(self) -> tuple[str, str]:
        return (self.campaign, self.no)


@dataclass
class PlogParse:
    sheet: str
    header_row: int
    columns: dict[str, int]
    rows: list[PlogRow] = field(default_factory=list)
    campaigns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def date_range(self) -> tuple[Optional[date], Optional[date]]:
        ds = [r.post_date for r in self.rows if r.post_date]
        return (min(ds), max(ds)) if ds else (None, None)


PLOG_REQUIRED = {"name", "postlink"}


def parse_plog(path: str) -> PlogParse:
    wb = load_workbook(path, data_only=True)
    found = None
    for ws in wb.worksheets:
        hit = find_header_row(ws, PLOG_REQUIRED)
        if hit:
            found = (ws, *hit)
            break
    if not found:
        raise ValueError(
            "KOL parse failed: no sheet has a header row containing both "
            "'NAME' and 'POST LINK' within the first "
            f"{HEADER_SCAN_ROWS} rows."
        )
    ws, header_row, cols = found
    result = PlogParse(sheet=ws.title, header_row=header_row, columns=cols)

    def col(key: str) -> Optional[int]:
        return cols.get(key)

    c_no, c_campaign, c_name = col("no"), col("campaign"), col("name")
    c_date, c_link = col("postdate"), col("postlink")
    c_like, c_coll, c_comm = col("like"), col("collection"), col("comment")
    c_impr, c_ttl = col("impression"), col("ttlengagement")

    current_campaign = ""
    seen_keys: set[tuple[str, str]] = set()
    bad_dates = 0
    consecutive_blank = 0
    for row in ws.iter_rows(min_row=header_row + 1):
        r = row[0].row

        def get(column):
            return ws.cell(row=r, column=column).value if column else None

        name = cell_str(get(c_name))
        link_cell = ws.cell(row=r, column=c_link) if c_link else None
        link = ""
        if link_cell is not None:
            if link_cell.hyperlink and link_cell.hyperlink.target:
                link = str(link_cell.hyperlink.target).strip()
            else:
                link = cell_str(link_cell.value)
        if not name and not link:
            # blank separator / spacer rows — but styled-empty phantom rows can
            # inflate ws.max_row to the sheet limit, so stop after a long run.
            consecutive_blank += 1
            if consecutive_blank >= MAX_CONSECUTIVE_BLANK_ROWS:
                break
            continue
        consecutive_blank = 0

        raw_date = get(c_date)
        parsed_date = to_date(raw_date)
        if parsed_date is None and cell_str(raw_date):
            bad_dates += 1
            if bad_dates <= 5:
                result.warnings.append(
                    f"KOL row {r}: POST DATE {cell_str(raw_date)!r} could not "
                    "be parsed — date-based checks are skipped for this row."
                )

        campaign = cell_str(get(c_campaign))
        if campaign:
            current_campaign = campaign
        no = cell_str(get(c_no))
        prow = PlogRow(
            campaign=current_campaign,
            no=no,
            name=name,
            post_date=parsed_date,
            post_link=link,
            like=to_int(get(c_like)),
            collection=to_int(get(c_coll)),
            comment=to_int(get(c_comm)),
            impression=to_int(get(c_impr)),
            ttl_engagement=to_int(get(c_ttl)),
            excel_row=r,
        )
        if prow.key in seen_keys:
            result.warnings.append(
                f"Duplicate row identity (CAMPAIGN={prow.campaign!r}, NO={prow.no!r}) "
                f"at sheet row {r} — each row is still annotated individually "
                "(rows are tracked by sheet row), but check the source data."
            )
        seen_keys.add(prow.key)
        if prow.campaign and prow.campaign not in result.campaigns:
            result.campaigns.append(prow.campaign)
        result.rows.append(prow)

    if bad_dates > 5:
        result.warnings.append(
            f"KOL: {bad_dates} rows in total had unparseable POST DATE values."
        )
    if not result.rows:
        result.warnings.append("KOL sheet parsed but contained no data rows.")
    return result


# ----------------------------------------------------------------------- DMR

@dataclass
class DmrRow:
    blogger: str
    username: str          # XHS author/user id — treated as an opaque string
    post_id: str           # normalized (lowercase) join key
    post_id_raw: str
    post_date: Optional[datetime]
    likes_retweet: Optional[int]
    share_favorites: Optional[int]
    engagement: Optional[int]
    comments: Optional[int]
    link_target: str       # hyperlink target of the Link cell, if any
    link_embedded_post_id: str
    excel_row: int
    weighted_eng: Optional[float] = None  # "WEIGHTED ENG." column


@dataclass
class DmrParse:
    sheet: str
    header_row: int
    columns: dict[str, int]
    rows: list[DmrRow] = field(default_factory=list)
    window_from: Optional[date] = None
    window_to: Optional[date] = None
    metadata_text: str = ""
    warnings: list[str] = field(default_factory=list)


DMR_REQUIRED = {"blogger", "postid"}

# The To-date must stop at a comma or the ' | ' joiner we insert between
# metadata cells — otherwise trailing metadata leaks into the capture.
_WINDOW_RE = re.compile(r"From\s+([^,|]+?)\s+To\s+([^,|]+)", re.IGNORECASE)


def _embedded_post_id(link_target: str) -> str:
    """The DMR Link column carries https://www.dmr.st/redi.html?url=<urlencoded
    xiaohongshu.com/discovery/item/{PostID}> — extract that PostID."""
    if not link_target:
        return ""
    try:
        parsed = urlparse(link_target)
        qs = parse_qs(parsed.query)
        inner = unquote(qs.get("url", [""])[0]) or link_target
    except ValueError:
        inner = link_target
    m = HEX24.search(inner)
    return m.group(1).lower() if m else ""


def parse_dmr(path: str) -> DmrParse:
    wb = load_workbook(path, data_only=True)
    found = None
    for ws in wb.worksheets:
        hit = find_header_row(ws, DMR_REQUIRED)
        if hit:
            found = (ws, *hit)
            break
    if not found:
        raise ValueError(
            "DMR parse failed: no sheet has a header row containing both "
            f"'Blogger' and 'PostID' within the first {HEADER_SCAN_ROWS} rows."
        )
    ws, header_row, cols = found
    result = DmrParse(sheet=ws.title, header_row=header_row, columns=cols)

    # Metadata rows above the header carry the export window
    # ("... Top Bloggers - From <date> To <date>"). Guard header_row == 1:
    # openpyxl treats max_row=0 as "unset" and would iterate the whole sheet.
    meta_parts = []
    if header_row > 1:
        for row in ws.iter_rows(min_row=1, max_row=header_row - 1):
            for cell in row:
                s = cell_str(cell.value)
                if s:
                    meta_parts.append(s)
    result.metadata_text = " | ".join(meta_parts)
    m = _WINDOW_RE.search(result.metadata_text)
    if m:
        result.window_from = to_date(m.group(1))
        result.window_to = to_date(m.group(2))
    if not (result.window_from and result.window_to):
        result.warnings.append(
            "Could not parse the DMR export date window from the metadata rows; "
            "out-of-window checks are disabled for this run."
        )

    def col(key: str) -> Optional[int]:
        return cols.get(key)

    c_blogger, c_user, c_pid = col("blogger"), col("username"), col("postid")
    c_pdate = col("postdate")
    c_likes, c_shares = col("likes_retweet"), col("share_favorites")
    c_eng, c_comm, c_link = col("engagement"), col("comments"), col("link")
    # header_key("WEIGHTED ENG.") == "weightedeng." — accept spelling drift
    c_weng = next((cols[k] for k in ("weightedeng.", "weightedeng",
                                     "weightedengagement") if k in cols), None)

    non_hex_pids = 0
    consecutive_blank = 0
    for row in ws.iter_rows(min_row=header_row + 1):
        r = row[0].row

        def get(column):
            return ws.cell(row=r, column=column).value if column else None

        blogger = cell_str(get(c_blogger))
        pid_raw = cell_str(get(c_pid))
        if not blogger and not pid_raw:
            consecutive_blank += 1
            if consecutive_blank >= MAX_CONSECUTIVE_BLANK_ROWS:
                break
            continue
        consecutive_blank = 0
        link_cell = ws.cell(row=r, column=c_link) if c_link else None
        link_target = extract_link_target(link_cell)
        embedded = _embedded_post_id(link_target)
        pid = pid_raw.lower()
        if pid_raw and not is_hex24(pid_raw):
            # Row is kept (a deviating PostID is itself a finding), but it can
            # never join — tell the operator instead of failing silently.
            non_hex_pids += 1
            if non_hex_pids <= 5:
                result.warnings.append(
                    f"DMR row {r}: PostID {pid_raw!r} is not a 24-char hex note "
                    "id — this row cannot join against resolved links."
                )
        drow = DmrRow(
            blogger=blogger,
            username=cell_str(get(c_user)),
            post_id=pid,
            post_id_raw=pid_raw,
            post_date=to_datetime(get(c_pdate)),
            likes_retweet=to_int(get(c_likes)),
            share_favorites=to_int(get(c_shares)),
            engagement=to_int(get(c_eng)),
            comments=to_int(get(c_comm)),
            link_target=link_target,
            link_embedded_post_id=embedded,
            excel_row=r,
            weighted_eng=to_float(get(c_weng)),
        )
        if embedded and pid and embedded != pid:
            result.warnings.append(
                f"DMR row {r}: Link hyperlink embeds PostID {embedded} but the "
                f"PostID column says {pid} — using the PostID column for the join."
            )
        result.rows.append(drow)

    if non_hex_pids > 5:
        result.warnings.append(
            f"DMR: {non_hex_pids} rows in total had non-hex PostID values."
        )
    if c_user is None:
        result.warnings.append(
            "DMR has no 'Username' column — blogger-presence checks (无博主 vs "
            "无帖子) cannot be decided deterministically for this file."
        )
    elif result.rows and not any(r.username for r in result.rows):
        result.warnings.append(
            "DMR 'Username' column is entirely empty — blogger-presence checks "
            "(无博主 vs 无帖子) cannot be decided deterministically for this file."
        )
    if not result.rows:
        result.warnings.append("DMR sheet parsed but contained no data rows.")
    return result
