"""Transactional email via the Resend HTTP API.

A fixed, trusted endpoint (api.resend.com) — unrelated to the SSRF allowlist,
which guards user-supplied XHS URLs. When RESEND_API_KEY is unset every send
is a no-op that returns False, so the account flows degrade gracefully rather
than erroring. Bodies are bilingual (EN + 中文) since the team is mixed.
"""
from __future__ import annotations

import html
from typing import Optional

import httpx

from .. import config

_API = "https://api.resend.com/emails"
_TIMEOUT = 12.0


def send(to: str, subject: str, html_body: str, text_body: str) -> bool:
    """Return True if Resend accepted the message. Never raises."""
    if not config.RESEND_API_KEY:
        return False
    try:
        resp = httpx.post(
            _API, timeout=_TIMEOUT,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": config.EMAIL_FROM, "to": [to], "subject": subject,
                  "html": html_body, "text": text_body},
        )
        return resp.status_code < 300
    except httpx.HTTPError:
        return False


def _wrap(intro_en: str, intro_zh: str, button_label: str, link: str,
          ttl_note_en: str, ttl_note_zh: str) -> tuple[str, str]:
    safe = html.escape(link, quote=True)
    html_body = f"""\
<div style="font-family:Helvetica,Arial,sans-serif;max-width:520px;margin:auto;color:#111">
  <p style="font:400 20px/1 Georgia,serif;letter-spacing:.3em;text-transform:uppercase;text-align:center">DMR&nbsp;Reconciler</p>
  <p style="font-size:15px">{html.escape(intro_en)}</p>
  <p style="font-size:14px;color:#6b6b6b">{html.escape(intro_zh)}</p>
  <p style="text-align:center;margin:28px 0">
    <a href="{safe}" style="background:#111;color:#fff;text-decoration:none;
       padding:12px 26px;font-size:13px;letter-spacing:.16em;text-transform:uppercase">{html.escape(button_label)}</a></p>
  <p style="font-size:12px;color:#6b6b6b">{html.escape(ttl_note_en)}<br>{html.escape(ttl_note_zh)}</p>
  <p style="font-size:12px;color:#9a9a9a;word-break:break-all">{safe}</p>
</div>"""
    text_body = (f"{intro_en}\n{intro_zh}\n\n{button_label}: {link}\n\n"
                 f"{ttl_note_en}\n{ttl_note_zh}")
    return html_body, text_body


def send_invite(to: str, link: str, ttl_hours: int,
                inviter: Optional[str] = None) -> bool:
    by = f" by {inviter}" if inviter else ""
    html_body, text_body = _wrap(
        f"You've been invited{by} to the DMR Reconciler. "
        "Click below to set your password and activate your account.",
        "你被邀请加入 DMR Reconciler。点击下方按钮设置密码并激活账号。",
        "Set your password", link,
        f"This link works once and expires in {ttl_hours} hours.",
        f"该链接仅可使用一次，{ttl_hours} 小时后失效。")
    return send(to, "Set up your DMR Reconciler account · 激活你的账号",
                html_body, text_body)


def send_verify(to: str, link: str, ttl_hours: int) -> bool:
    html_body, text_body = _wrap(
        "Confirm this address for your DMR Reconciler account. Until you do, "
        "it cannot be used to reset your password.",
        "请确认此邮箱属于你的 DMR Reconciler 账号。未确认前，该邮箱无法用于重置密码。",
        "Confirm this email", link,
        f"This link works once and expires in {ttl_hours} hours.",
        f"该链接仅可使用一次，{ttl_hours} 小时后失效。")
    return send(to, "Confirm your DMR Reconciler email · 确认邮箱",
                html_body, text_body)


def send_reset(to: str, link: str, ttl_hours: int) -> bool:
    html_body, text_body = _wrap(
        "A password reset was requested for your DMR Reconciler account. "
        "Click below to choose a new password. If this wasn't you, ignore "
        "this email — your password stays unchanged.",
        "有人为你的 DMR Reconciler 账号申请了重置密码。点击下方按钮设置新密码。"
        "若非本人操作，请忽略本邮件，密码不会改变。",
        "Reset your password", link,
        f"This link works once and expires in {ttl_hours} hours.",
        f"该链接仅可使用一次，{ttl_hours} 小时后失效。")
    return send(to, "Reset your DMR Reconciler password · 重置密码",
                html_body, text_body)
