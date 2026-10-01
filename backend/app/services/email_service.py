"""
Phase 20: Email Service for CircularFlow AI
============================================

Sends institutional email notifications when a circular is officially published.

Provider: Resend (https://resend.com) — HTTP API, no SMTP required.
Fallback:  SMTP via Python smtplib if SMTP_HOST is configured instead.

Configuration (backend/.env):
    EMAIL_PROVIDER_API_KEY   – Resend API key  (e.g. re_xxxxxxxxxxxx)
    EMAIL_FROM               – Sender address  (e.g. noreply@vignan.ac.in)
    EMAIL_FROM_NAME          – Sender display name (e.g. CircularFlow AI)
    EMAIL_ENABLED            – Set to "false" to silence all emails (default: true)

    # SMTP fallback (only used when EMAIL_PROVIDER_API_KEY is absent)
    SMTP_HOST                – SMTP server hostname
    SMTP_PORT                – SMTP port (default: 587)
    SMTP_USER                – SMTP username
    SMTP_PASSWORD            – SMTP password
    SMTP_TLS                 – true/false (default: true)

SECURITY:
  - Credentials are read only from environment variables.
  - API keys are NEVER logged or returned in API responses.
  - If configuration is missing, emails are silently skipped and the
    circular publication continues uninterrupted.
"""
import os
import re
import uuid
import logging
import smtplib
import json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional

import urllib.request
import urllib.error

from sqlalchemy.orm import Session

from ..models.email_delivery_logs import EmailDeliveryLog
from ..models.audit_logs import AuditLog

logger = logging.getLogger(__name__)

# Demo test recipient for circular publication testing
DEMO_TEST_RECIPIENT_EMAIL = "vu.241fa04746@gmail.com"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration helpers (no secrets in logs)
# ─────────────────────────────────────────────────────────────────────────────

def _get_config() -> dict:
    return {
        "api_key": os.getenv("EMAIL_PROVIDER_API_KEY", "").strip(),
        "from_addr": os.getenv("EMAIL_FROM", "noreply@vignan.ac.in").strip(),
        "from_name": os.getenv("EMAIL_FROM_NAME", "CircularFlow AI — Vignan's University").strip(),
        "enabled": os.getenv("EMAIL_ENABLED", "true").strip().lower() not in ("false", "0", "no"),
        "smtp_host": os.getenv("SMTP_HOST", "").strip(),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "smtp_user": os.getenv("SMTP_USER", "").strip(),
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "smtp_tls": os.getenv("SMTP_TLS", "true").strip().lower() not in ("false", "0", "no"),
        "frontend_url": os.getenv("FRONTEND_URL", "https://circularflow.vignan.ac.in").strip(),
    }


def _is_configured(config: dict) -> bool:
    """Returns True if at least one delivery method is usable."""
    return bool(config["api_key"] or config["smtp_host"])


class ResendAPIError(Exception):
    """Structured exception for Resend API failures to capture diagnostics safely."""
    def __init__(
        self,
        status_code: Optional[int],
        error_name: str,
        error_message: str,
        response_body: str = "",
    ):
        self.status_code = status_code
        self.error_name = error_name
        self.error_message = error_message
        self.response_body = response_body
        summary = f"Resend HTTP {status_code}"
        if error_name:
            summary += f" [{error_name}]"
        if error_message:
            summary += f": {error_message}"
        elif response_body:
            summary += f": {response_body[:300]}"
        super().__init__(summary)


def _sanitize_error_text(
    text: str,
    *,
    api_key: str = "",
    smtp_password: str = "",
) -> str:
    """Ensure no API keys, Bearer tokens, or passwords appear in logs or error messages."""
    if not text:
        return ""
    sanitized = text
    # Strip exact API key if non-empty
    if api_key and len(api_key.strip()) >= 4:
        sanitized = sanitized.replace(api_key.strip(), "[REDACTED_API_KEY]")
    # Strip exact SMTP password if non-empty
    if smtp_password and len(smtp_password.strip()) >= 4:
        sanitized = sanitized.replace(smtp_password.strip(), "[REDACTED_PASSWORD]")
    # Redact Authorization: Bearer <token>
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", sanitized, flags=re.IGNORECASE)
    # Redact any Resend API keys matching re_<alphanumeric>
    sanitized = re.sub(r"\bre_[A-Za-z0-9_]{10,}\b", "[REDACTED_RESEND_KEY]", sanitized)
    return sanitized


# ─────────────────────────────────────────────────────────────────────────────
# HTML email template
# ─────────────────────────────────────────────────────────────────────────────

def _build_html(
    *,
    title: str,
    ref_no: str,
    department: str,
    priority: str,
    effective_date: str,
    summary: str,
    published_by: str,
    frontend_url: str,
    requires_action: bool = False,
) -> str:
    priority_color = {
        "Critical": "#dc2626",
        "High":     "#ea580c",
        "Medium":   "#ca8a04",
        "Low":      "#16a34a",
    }.get(priority, "#6366f1")

    action_block = ""
    if requires_action:
        action_block = f"""
        <div style="background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:16px;margin:20px 0;">
          <p style="margin:0;font-size:14px;color:#713f12;">
            <strong>⚠ Action Required:</strong> Please log in to CircularFlow AI, review this circular,
            and submit your digital acknowledgement before the effective date.
          </p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Inter,Segoe UI,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1e3a8a,#4f46e5);padding:28px 32px;">
            <p style="margin:0;color:#bfdbfe;font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:600;">
              Vignan's Foundation for Science, Technology &amp; Research
            </p>
            <h1 style="margin:6px 0 0;color:#ffffff;font-size:22px;font-weight:800;letter-spacing:-.02em;">
              CircularFlow AI
            </h1>
            <p style="margin:4px 0 0;color:#a5b4fc;font-size:12px;">Institutional Governance OS</p>
          </td>
        </tr>

        <!-- Badge -->
        <tr>
          <td style="padding:24px 32px 0;">
            <span style="display:inline-block;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;
                         border-radius:6px;padding:4px 12px;font-size:11px;font-weight:700;letter-spacing:.08em;">
              NEW INSTITUTIONAL CIRCULAR
            </span>
          </td>
        </tr>

        <!-- Title -->
        <tr>
          <td style="padding:16px 32px 0;">
            <h2 style="margin:0;color:#0f172a;font-size:20px;font-weight:700;line-height:1.3;">
              {title}
            </h2>
          </td>
        </tr>

        <!-- Meta grid -->
        <tr>
          <td style="padding:20px 32px 0;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="50%" style="padding-bottom:12px;">
                  <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Reference No.</p>
                  <p style="margin:4px 0 0;font-size:14px;color:#1e293b;font-weight:700;font-family:monospace;">{ref_no}</p>
                </td>
                <td width="50%" style="padding-bottom:12px;">
                  <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Priority</p>
                  <p style="margin:4px 0 0;font-size:14px;font-weight:700;color:{priority_color};">{priority}</p>
                </td>
              </tr>
              <tr>
                <td width="50%" style="padding-bottom:12px;">
                  <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Department</p>
                  <p style="margin:4px 0 0;font-size:14px;color:#1e293b;font-weight:600;">{department}</p>
                </td>
                <td width="50%" style="padding-bottom:12px;">
                  <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Effective Date</p>
                  <p style="margin:4px 0 0;font-size:14px;color:#1e293b;font-weight:600;">{effective_date}</p>
                </td>
              </tr>
              <tr>
                <td colspan="2">
                  <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Published By</p>
                  <p style="margin:4px 0 0;font-size:14px;color:#1e293b;font-weight:600;">{published_by}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Divider -->
        <tr><td style="padding:16px 32px 0;"><hr style="border:none;border-top:1px solid #e2e8f0;margin:0;"></td></tr>

        <!-- Summary -->
        <tr>
          <td style="padding:20px 32px 0;">
            <p style="margin:0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;font-weight:600;">Summary</p>
            <p style="margin:8px 0 0;font-size:14px;color:#334155;line-height:1.6;">{summary}</p>
          </td>
        </tr>

        {action_block}

        <!-- CTA -->
        <tr>
          <td style="padding:24px 32px;">
            <a href="{frontend_url}/circulars"
               style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#6366f1);color:#ffffff;
                      text-decoration:none;font-size:14px;font-weight:700;padding:12px 28px;
                      border-radius:8px;letter-spacing:.02em;">
              View in CircularFlow AI →
            </a>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;padding:20px 32px;border-top:1px solid #e2e8f0;">
            <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6;">
              This is an automated notification from <strong>CircularFlow AI</strong>, the institutional
              governance platform of Vignan's Foundation for Science, Technology &amp; Research.<br>
              Please do not reply to this email. For queries, contact your department administrator.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_plain(
    *,
    title: str,
    ref_no: str,
    department: str,
    priority: str,
    effective_date: str,
    summary: str,
    published_by: str,
    frontend_url: str,
    requires_action: bool = False,
) -> str:
    action_note = (
        "\n⚠ ACTION REQUIRED: Log in and acknowledge this circular before the effective date.\n"
        if requires_action else ""
    )
    return f"""CircularFlow AI — Institutional Governance OS
Vignan's Foundation for Science, Technology & Research
══════════════════════════════════════════════════════

NEW INSTITUTIONAL CIRCULAR

Title:          {title}
Reference No.:  {ref_no}
Department:     {department}
Priority:       {priority}
Effective Date: {effective_date}
Published By:   {published_by}
{action_note}
Summary:
{summary}

View in CircularFlow AI:
{frontend_url}/circulars

──────────────────────────────────────────────────────
This is an automated notification. Do not reply.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Low-level delivery functions
# ─────────────────────────────────────────────────────────────────────────────

def _send_via_resend(
    *,
    api_key: str,
    from_addr: str,
    from_name: str,
    to_email: str,
    to_name: str,
    subject: str,
    html: str,
    plain: str,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: Optional[str] = None,
) -> dict:
    """Send using Resend REST API (no extra library needed — pure urllib)."""
    clean_api_key = (api_key or "").strip()
    attachments = []
    if pdf_bytes and pdf_filename:
        import base64
        attachments = [{
            "filename": pdf_filename,
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
        }]

    sender = f"{from_name.strip()} <{from_addr.strip()}>" if from_name and from_name.strip() else from_addr.strip()
    recipient = f"{to_name.strip()} <{to_email.strip()}>" if to_name and to_name.strip() else to_email.strip()

    payload: dict = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": plain,
    }
    if attachments:
        payload["attachments"] = attachments

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": f"Bearer {clean_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "resend-python/2.6.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status not in (200, 201):
                raise ResendAPIError(
                    status_code=resp.status,
                    error_name="unexpected_status",
                    error_message=f"HTTP {resp.status}",
                    response_body=_sanitize_error_text(body, api_key=clean_api_key),
                )
            try:
                return json.loads(body)
            except Exception:
                return {"raw": body}
    except urllib.error.HTTPError as err:
        status_code = err.code
        err_body = ""
        try:
            err_body = err.read().decode("utf-8", errors="replace")
        except Exception:
            pass

        err_name = ""
        err_message = ""
        try:
            err_json = json.loads(err_body)
            err_name = str(err_json.get("name") or err_json.get("code") or "")
            err_message = str(err_json.get("message") or "")
        except Exception:
            pass

        sanitized_body = _sanitize_error_text(err_body, api_key=api_key)
        sanitized_message = _sanitize_error_text(err_message or str(err), api_key=api_key)

        raise ResendAPIError(
            status_code=status_code,
            error_name=err_name or f"HTTP_{status_code}",
            error_message=sanitized_message,
            response_body=sanitized_body,
        ) from None
    except urllib.error.URLError as err:
        sanitized_err = _sanitize_error_text(str(err.reason), api_key=api_key)
        raise RuntimeError(f"Network error connecting to Resend: {sanitized_err}") from None


def _send_via_smtp(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_tls: bool,
    from_addr: str,
    from_name: str,
    to_email: str,
    to_name: str,
    subject: str,
    html: str,
    plain: str,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: Optional[str] = None,
) -> None:
    """Send using Python smtplib (SMTP/STARTTLS fallback)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if pdf_bytes and pdf_filename:
        attachment = MIMEBase("application", "pdf")
        attachment.set_payload(pdf_bytes)
        encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", "attachment", filename=pdf_filename)
        msg.attach(attachment)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
        if smtp_tls:
            server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(from_addr, [to_email], msg.as_string())


# ─────────────────────────────────────────────────────────────────────────────
# Main public API
# ─────────────────────────────────────────────────────────────────────────────

def send_single_notification(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    title: str,
    ref_no: str,
    department: str,
    priority: str,
    effective_date: str,
    summary: str,
    published_by: str,
    requires_action: bool = False,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: Optional[str] = None,
) -> None:
    """
    Attempt to send one notification email.
    Raises on failure so the caller can record FAILED status.
    """
    config = _get_config()
    if not config["enabled"]:
        logger.info("[EmailService] Email notifications disabled (EMAIL_ENABLED=false). Skipping.")
        return
    if not _is_configured(config):
        logger.info(
            "[EmailService] No email provider configured "
            "(set EMAIL_PROVIDER_API_KEY or SMTP_HOST). Skipping."
        )
        return

    html = _build_html(
        title=title, ref_no=ref_no, department=department, priority=priority,
        effective_date=effective_date, summary=summary, published_by=published_by,
        frontend_url=config["frontend_url"], requires_action=requires_action,
    )
    plain = _build_plain(
        title=title, ref_no=ref_no, department=department, priority=priority,
        effective_date=effective_date, summary=summary, published_by=published_by,
        frontend_url=config["frontend_url"], requires_action=requires_action,
    )

    if config["api_key"]:
        _send_via_resend(
            api_key=config["api_key"],
            from_addr=config["from_addr"],
            from_name=config["from_name"],
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            html=html,
            plain=plain,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
        )
    else:
        _send_via_smtp(
            smtp_host=config["smtp_host"],
            smtp_port=config["smtp_port"],
            smtp_user=config["smtp_user"],
            smtp_password=config["smtp_password"],
            smtp_tls=config["smtp_tls"],
            from_addr=config["from_addr"],
            from_name=config["from_name"],
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            html=html,
            plain=plain,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
        )


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email(email: Optional[str]) -> bool:
    """Validate email format."""
    if not email:
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


def resolve_circular_recipients(
    db: Session,
    *,
    circular_id: str,
    circular_ref: str,
    department: str,
    target_audience: List[str],
    published_by_email: Optional[str] = None,
) -> dict:
    """
    Resolve and deduplicate intended email recipients for a circular.

    Resolution Strategy:
    1. Existing Acknowledgements: Recipients pre-linked to this circular.
    2. Target Audience matching:
       - Universal broadcast keywords ("all campus occupants", "all staff", "all students", etc.)
       - Role-specific keywords (Faculty, HOD, Student, Staff)
       - Department-specific audience mentions
    3. Department matching: Recipients and Users matching circular's department.
    4. Governance leads: Registrar / Admin users.
    5. User joining: Resolves recipient emails to User.id where matching user records exist.
    6. Governance failsafe: If resolution is empty, falls back to Department HODs + Registrar.
    7. Publisher exclusion: Removes publisher email if other recipients exist to avoid self-notification.
    8. Demo recipient: Ensures demo test recipient is included for publication testing.

    Returns:
        dict: email -> {"name": str, "user_id": Optional[str], "source": str}
    """
    from ..models.recipients import Recipient
    from ..models.users import User
    from ..models.acknowledgements import Acknowledgement

    audience_list = [a.strip().lower() for a in (target_audience or []) if a and a.strip()]
    audience_text = " ".join(audience_list)

    # Universal broadcast detection
    universal_keywords = (
        "all", "all campus", "all campus occupants", "all staff", "all students",
        "all faculty", "all users", "entire university", "campus-wide",
        "everyone", "general", "all members", "all occupants", "all teaching faculty",
    )
    is_universal = any(
        kw == a or kw in audience_text
        for kw in universal_keywords
        for a in (audience_list or [""])
    ) if audience_list else False

    # Role-based audience flags
    target_faculty = is_universal or any(
        kw in audience_text for kw in ("faculty", "teaching", "professor", "prof", "academic", "dean")
    )
    target_students = is_universal or any(
        kw in audience_text for kw in ("student", "students", "scholar", "scholars")
    )
    target_hods = is_universal or any(
        kw in audience_text for kw in ("hod", "head", "heads", "chair", "department heads", "department hods")
    )
    target_staff = is_universal or any(
        kw in audience_text for kw in ("staff", "technician", "technicians", "support", "warden", "wardens", "squad", "security")
    )

    resolved: dict[str, dict] = {}

    # ── 1. Circular Acknowledgements (pre-seeded / explicit recipient links) ──
    acks = db.query(Acknowledgement).filter(Acknowledgement.circular_id == circular_id).all()
    for ack in acks:
        if ack.recipient_rel and _is_valid_email(ack.recipient_rel.email):
            em = ack.recipient_rel.email.strip().lower()
            resolved[em] = {
                "name": ack.recipient_rel.name or "",
                "user_id": None,
                "source": "acknowledgement",
            }

    # ── 2. Recipients Table Matching ─────────────────────────────────────────
    dept_lower = (department or "").strip().lower()
    all_recipients = db.query(Recipient).all()

    for r in all_recipients:
        if not _is_valid_email(r.email):
            continue

        em = r.email.strip().lower()
        r_role = (r.role or "").strip().lower()
        r_dept = (r.department or "").strip().lower()

        matched = False
        if is_universal:
            matched = True
        elif dept_lower and (dept_lower in r_dept or r_dept in dept_lower):
            matched = True
        elif target_hods and r_role == "hod":
            matched = True
        elif target_faculty and r_role in ("faculty", "hod"):
            matched = True
        elif target_students and r_role == "student":
            matched = True
        elif target_staff and r_role in ("staff", "technician"):
            matched = True

        if matched and em not in resolved:
            resolved[em] = {
                "name": r.name or "",
                "user_id": None,
                "source": "recipients_table",
            }

    # ── 3. Users Table Matching & Joining ────────────────────────────────────
    all_users = db.query(User).all()
    user_email_to_id: dict[str, str] = {}

    for u in all_users:
        if not _is_valid_email(u.email):
            continue
        u_em = u.email.strip().lower()
        user_email_to_id[u_em] = u.id

        u_role = (u.role or "").strip().lower()
        u_dept = (u.department_name or "").strip().lower()

        matched_user = False
        if is_universal:
            matched_user = True
        elif u_role in ("registrar", "admin"):
            matched_user = True
        elif dept_lower and (dept_lower in u_dept or u_dept in dept_lower):
            matched_user = True
        elif target_faculty and u_role == "faculty":
            matched_user = True
        elif target_students and u_role == "student":
            matched_user = True

        if matched_user:
            if u_em not in resolved:
                resolved[u_em] = {
                    "name": u.name or "",
                    "user_id": u.id,
                    "source": "users_table",
                }
            else:
                resolved[u_em]["user_id"] = u.id

    # Attach User.id to any recipient matched from the recipients table
    for em, data in resolved.items():
        if not data.get("user_id") and em in user_email_to_id:
            data["user_id"] = user_email_to_id[em]

    # ── 4. Governance Failsafe: Fallback to HODs + Registrar if no target recipients ──
    has_target_recipients = any(
        d.get("source") in ("acknowledgement", "recipients_table") for d in resolved.values()
    )
    if not has_target_recipients:
        logger.info(
            "[EmailService] No target recipients matched by audience/department for %s. "
            "Applying governance fallback (Department HODs + Registrar).",
            circular_ref,
        )
        for r in all_recipients:
            if _is_valid_email(r.email) and (r.role or "").strip().lower() == "hod":
                em = r.email.strip().lower()
                resolved[em] = {
                    "name": r.name or "",
                    "user_id": user_email_to_id.get(em),
                    "source": "fallback_hod",
                }

        for u in all_users:
            if _is_valid_email(u.email) and (u.role or "").strip().lower() in ("registrar", "admin"):
                em = u.email.strip().lower()
                resolved[em] = {
                    "name": u.name or "",
                    "user_id": u.id,
                    "source": "fallback_admin",
                }

    # ── 5. Exclude Publisher (avoid self-notification if others exist) ────────
    demo_email = os.getenv("DEMO_RECIPIENT_EMAIL", DEMO_TEST_RECIPIENT_EMAIL).strip().lower()

    if published_by_email and _is_valid_email(published_by_email):
        pub_em = published_by_email.strip().lower()
        # Do not exclude the demo recipient even if they are the publisher
        if pub_em in resolved and pub_em != demo_email:
            if len(resolved) > 1:
                resolved.pop(pub_em, None)
                logger.info(
                    "[EmailService] Excluded publisher %s from notification list (%d recipients remain).",
                    pub_em, len(resolved),
                )
            else:
                logger.info(
                    "[EmailService] Publisher %s is the sole resolved recipient for %s — retained.",
                    pub_em, circular_ref,
                )

    # ── 6. Demo / Test Recipient Resolution ──────────────────────────────────
    # Ensure vu.241fa04746@gmail.com is included as a recipient for demo testing
    if _is_valid_email(demo_email) and demo_email not in resolved:
        resolved[demo_email] = {
            "name": "Demo Test Recipient",
            "user_id": user_email_to_id.get(demo_email),
            "source": "demo_test_recipient",
        }
        logger.info(
            "[EmailService] Included demo test recipient %s for circular %s.",
            demo_email, circular_ref,
        )

    return resolved


def dispatch_circular_notifications(
    *,
    db: Session,
    circular_id: str,
    circular_ref: str,
    title: str,
    department: str,
    priority: str,
    effective_date: str,
    summary: str,
    published_by: str,
    published_by_email: Optional[str],
    target_audience: List[str],
    requires_action: bool = False,
    pdf_bytes: Optional[bytes] = None,
    pdf_filename: Optional[str] = None,
) -> dict:
    """
    Determine recipients and send notification emails for a published circular.

    This runs in a background task — errors are recorded, not raised to the HTTP layer.
    Returns a summary dict: {"sent": N, "failed": M, "skipped": K}
    """
    config = _get_config()
    logger.info(
        "[EmailService] Config check: EMAIL_ENABLED=%s, EMAIL_PROVIDER_API_KEY configured=%s, "
        "EMAIL_FROM='%s', EMAIL_FROM_NAME='%s'",
        config["enabled"],
        bool(config["api_key"] and config["api_key"].strip()),
        config["from_addr"],
        config["from_name"],
    )
    if not config["enabled"]:
        logger.info("[EmailService] Email notifications disabled (EMAIL_ENABLED=false). dispatch skipped.")
        _write_audit(db, circular_id, circular_ref, sent=0, failed=0, skipped=0,
                     reason="EMAIL_ENABLED=false")
        return {"sent": 0, "failed": 0, "skipped": 0}

    if not _is_configured(config):
        logger.info("[EmailService] Email not configured (no API key or SMTP). dispatch skipped.")
        _write_audit(db, circular_id, circular_ref, sent=0, failed=0, skipped=0,
                     reason="no provider configured")
        return {"sent": 0, "failed": 0, "skipped": 0}

    # ── Resolve Recipients ───────────────────────────────────────────────────
    recipient_map = resolve_circular_recipients(
        db,
        circular_id=circular_id,
        circular_ref=circular_ref,
        department=department,
        target_audience=target_audience,
        published_by_email=published_by_email,
    )

    valid_emails_count = len(recipient_map)
    users_with_id_count = sum(1 for d in recipient_map.values() if d.get("user_id"))

    logger.info(
        "[EmailService] Circular %s recipient resolution: "
        "department='%s', audience=%s -> "
        "%d valid email addresses, %d users linked, final recipient count: %d",
        circular_ref, department, target_audience,
        valid_emails_count, users_with_id_count, valid_emails_count,
    )

    if not recipient_map:
        reason = (
            f"No recipients resolved for department='{department}', "
            f"target_audience={target_audience}. Database contains no matching recipients or users."
        )
        logger.warning("[EmailService] %s - circular %s", reason, circular_ref)
        _write_audit(db, circular_id, circular_ref, sent=0, failed=0, skipped=0, reason=reason)
        return {"sent": 0, "failed": 0, "skipped": 0, "reason": reason}

    sent = failed = skipped = 0
    subject = f"[New Circular] {title} ({circular_ref})"

    for email, data in recipient_map.items():
        name = data.get("name", "")
        user_id = data.get("user_id")

        log_id = str(uuid.uuid4())
        log_entry = EmailDeliveryLog(
            id=log_id,
            circular_id=circular_id,
            circular_ref=circular_ref,
            recipient_email=email,
            recipient_name=name,
            recipient_user_id=user_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )
        db.add(log_entry)
        db.flush()  # get the row into DB before attempt

        try:
            send_single_notification(
                to_email=email,
                to_name=name,
                subject=subject,
                title=title,
                ref_no=circular_ref,
                department=department,
                priority=priority,
                effective_date=effective_date,
                summary=summary,
                published_by=published_by,
                requires_action=requires_action,
                pdf_bytes=pdf_bytes,
                pdf_filename=pdf_filename,
            )
            log_entry.status = "SENT"
            log_entry.sent_at = datetime.now(timezone.utc)
            sent += 1
            logger.info("[EmailService] Sent to %s for circular %s", email, circular_ref)
        except Exception as exc:  # noqa: BLE001
            log_entry.status = "FAILED"
            api_key = config.get("api_key", "")
            smtp_pass = config.get("smtp_password", "")

            if isinstance(exc, ResendAPIError):
                diag_msg = (
                    f"Resend HTTP {exc.status_code} | "
                    f"code: '{exc.error_name}' | "
                    f"message: '{exc.error_message}'"
                )
                if exc.response_body and exc.response_body != exc.error_message:
                    diag_msg += f" | body: {exc.response_body[:400]}"
            else:
                diag_msg = str(exc)

            safe_err = _sanitize_error_text(diag_msg, api_key=api_key, smtp_password=smtp_pass)
            log_entry.error_message = safe_err[:1000]
            failed += 1
            logger.error("[EmailService] Failed to send to %s: %s", email, safe_err)

        db.commit()

    _write_audit(db, circular_id, circular_ref, sent=sent, failed=failed, skipped=skipped)
    logger.info(
        "[EmailService] Dispatch complete for %s — sent=%d failed=%d skipped=%d",
        circular_ref, sent, failed, skipped,
    )
    return {"sent": sent, "failed": failed, "skipped": skipped}


def _write_audit(
    db: Session,
    circular_id: str,
    circular_ref: str,
    *,
    sent: int,
    failed: int,
    skipped: int,
    reason: str = "",
) -> None:
    """Record EMAIL_NOTIFICATION_SENT / EMAIL_NOTIFICATION_FAILED / EMAIL_NOTIFICATION_SKIPPED in audit_logs."""
    try:
        event_type = "EMAIL_NOTIFICATION_SENT" if failed == 0 and sent > 0 else (
            "EMAIL_NOTIFICATION_FAILED" if failed > 0 else "EMAIL_NOTIFICATION_SKIPPED"
        )
        description = (
            f"Email notifications for {circular_ref}: "
            f"{sent} sent, {failed} failed, {skipped} skipped."
        )
        if reason:
            description += f" Reason: {reason}"

        log = AuditLog(
            id=str(uuid.uuid4()),
            event_type=event_type,
            description=description,
            related_circular_id=circular_id,
            event_metadata={
                "circular_ref": circular_ref,
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
                "reason": reason,
            },
        )
        db.add(log)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("[EmailService] Failed to write audit log: %s", exc)


def test_resend_connectivity() -> dict:
    """
    Send exactly one test email using the existing Resend integration.
    From: onboarding@resend.dev
    To: delivered@resend.dev
    Subject: CircularFlow AI Resend Connectivity Test
    Body: This is a temporary connectivity test for CircularFlow AI.

    Returns a dict with:
      - success: bool
      - provider: "resend"
      - status_code: int or None
      - error/details/response_body: sanitized strings (if failed)
      - message: str (if successful)
      - request_info: sanitized request configuration info (NO secrets)

    Never returns or logs EMAIL_PROVIDER_API_KEY, JWT, password, or any secret.
    Creates NO database records.
    """
    config = _get_config()
    api_key = config.get("api_key", "").strip()

    from_addr = "onboarding@resend.dev"
    from_name = "CircularFlow AI"
    to_email = "delivered@resend.dev"
    to_name = "Resend Test Delivery"
    subject = "CircularFlow AI Resend Connectivity Test"
    body_text = "This is a temporary connectivity test for CircularFlow AI."
    html_text = f"<p>{body_text}</p>"

    # Sanitized request configuration info (guaranteed secret-free)
    request_info = {
        "endpoint": "https://api.resend.com/emails",
        "method": "POST",
        "from": f"{from_name} <{from_addr}>",
        "to": to_email,
        "user_agent": "resend-python/2.6.0",
        "api_key_configured": bool(api_key),
        "api_key_format_valid": api_key.startswith("re_") if api_key else False,
        "configured_from_address": config.get("from_addr", ""),
        "configured_from_name": config.get("from_name", ""),
        "email_enabled": config.get("enabled", True),
    }

    if not api_key:
        return {
            "success": False,
            "provider": "resend",
            "status_code": None,
            "error": "EMAIL_PROVIDER_API_KEY is not configured on the server.",
            "request_info": request_info,
        }

    try:
        resp_data = _send_via_resend(
            api_key=api_key,
            from_addr=from_addr,
            from_name=from_name,
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            html=html_text,
            plain=body_text,
        )
        email_id = resp_data.get("id") if isinstance(resp_data, dict) else None
        res = {
            "success": True,
            "provider": "resend",
            "status_code": 200,
            "message": "Test email successfully accepted by Resend API.",
            "request_info": request_info,
        }
        if email_id:
            res["email_id"] = email_id
        return res
    except ResendAPIError as exc:
        safe_msg = _sanitize_error_text(exc.error_message, api_key=api_key)
        safe_body = _sanitize_error_text(exc.response_body, api_key=api_key)
        res = {
            "success": False,
            "provider": "resend",
            "status_code": exc.status_code,
            "error_code": exc.error_name,
            "error": safe_msg or f"HTTP {exc.status_code}",
            "response_body": safe_body[:500] if safe_body else None,
            "request_info": request_info,
        }
        if safe_body and safe_body != safe_msg:
            res["details"] = safe_body[:400]
        return res
    except Exception as exc:
        safe_err = _sanitize_error_text(str(exc), api_key=api_key)
        return {
            "success": False,
            "provider": "resend",
            "status_code": None,
            "error": safe_err,
            "request_info": request_info,
        }

