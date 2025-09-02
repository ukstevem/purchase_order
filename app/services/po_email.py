# app/services/po_email.py
from __future__ import annotations
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from app.integrations.outlook_graph import create_draft_with_attachment


def _po_num_str(po_number: int | str) -> str:
    try:
        return f"{int(po_number):06d}"
    except Exception:
        return str(po_number)


def _extract_project_number(po: Dict[str, Any]) -> str:
    # Adjust these key paths to match your data shape
    return (
        po.get("projectnumber")
        or po.get("project_number")
        or (po.get("project") or {}).get("projectnumber")
        or "UNKNOWN-PROJECT"
    )


def _extract_supplier_email(po: Dict[str, Any]) -> Optional[str]:
    # Adjust to your schema
    return (
        po.get("supplier_email")
        or (po.get("supplier") or {}).get("email")
        or None
    )


def build_subject_and_body(project_number: str, po_num_str: str) -> tuple[str, str]:
    subject = f"{project_number} PO {po_num_str}"
    body_text = (
        f"Please find attached PO {po_num_str} for previously quoted materials, "
        "please confirm as soon as possible and notify of any late or unavailable items.\n\n"
        "Best Regards,"
    )
    return subject, body_text


def try_create_po_draft(
    archive_path: str | Path,
    po: Dict[str, Any],
    mailbox_upn: Optional[str] = None,
    to_recipients: Optional[List[str]] = None,
    cc_recipients: Optional[List[str]] = None,
    feature_flag_env: str = "EMAIL_DRAFT_ON_PO",
) -> Optional[dict]:
    """
    Creates an Outlook draft with the archived PDF attached.
    Safe to call from routes; failures are logged and won't raise.

    Returns the created draft (dict) or None on skip/failure.
    """
    # Feature flag
    if os.environ.get(feature_flag_env, "1").lower() not in {"1", "true", "yes"}:
        logging.info("PO email draft creation is disabled by feature flag.")
        return None

    # Validate archive path
    if not archive_path or not Path(archive_path).exists():
        logging.warning("Archive path missing or not found; skipping Outlook draft creation.")
        return None

    # Mailbox
    mailbox_upn = mailbox_upn or os.environ.get("MS_OUTLOOK_MAILBOX")
    if not mailbox_upn:
        logging.warning("MS_OUTLOOK_MAILBOX not set; skipping Outlook draft creation.")
        return None

    # Data
    po_number = po.get("po_number") or po.get("id") or "UNKNOWN"
    po_num_str = _po_num_str(po_number)
    project_number = _extract_project_number(po)

    subject, body_text = build_subject_and_body(project_number, po_num_str)

    # Recipients (optional)
    if to_recipients is None:
        supplier_email = _extract_supplier_email(po)
        to_recipients = [supplier_email] if supplier_email else []

    try:
        draft = create_draft_with_attachment(
            mailbox_upn=mailbox_upn,
            subject=subject,
            body_text=body_text,
            pdf_path=str(archive_path),
            to_recipients=to_recipients,
            cc_recipients=cc_recipients or [],
        )
        logging.info(
            "Outlook draft created",
            extra={"subject": draft.get("subject"), "webLink": draft.get("webLink")}
        )
        return draft
    except Exception as e:
        logging.exception(f"Failed to create Outlook draft for PO {po_num_str}: {e}")
        return None
