# app/services/po_email.py
from __future__ import annotations
import os
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

from app.integrations.outlook_graph import create_draft_with_attachment

# How long a lock is considered "fresh" (seconds)
DEFAULT_LOCK_TTL = int(os.environ.get("PO_EMAIL_LOCK_TTL_SECONDS", "120"))
# Where to store lock files
DEFAULT_LOCK_DIR = os.environ.get("PO_EMAIL_LOCK_DIR", "/tmp/po_email_locks")


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


def _lock_path(lock_key: str) -> Path:
    lock_dir = Path(DEFAULT_LOCK_DIR)
    lock_dir.mkdir(parents=True, exist_ok=True)
    # keep it filesystem-safe
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in lock_key)
    return lock_dir / f"{safe}.lock"


def _acquire_singleflight(lock_key: str, ttl_seconds: int = DEFAULT_LOCK_TTL) -> bool:
    """
    Try to acquire a single-flight lock for this key.
    Returns True if this caller holds the lock; False if a fresh lock already exists.
    Lock is a small file whose mtime indicates freshness.
    """
    p = _lock_path(lock_key)
    now = time.time()

    # If an existing fresh lock is present, do NOT proceed.
    if p.exists():
        try:
            mtime = p.stat().st_mtime
            if (now - mtime) < ttl_seconds:
                return False  # another call very recently created/scheduled the draft
            # stale lock -> replace
        except Exception:
            # if anything odd, try to replace
            pass

    # (Re)create/refresh the lock atomically
    try:
        # Write our timestamp; not strictly necessary but handy for debugging
        with open(p, "w") as f:
            f.write(str(int(now)))
        # Double-check mtime is "now-ish"
        os.utime(p, times=(now, now))
        return True
    except Exception as e:
        logging.warning(f"Could not create lock file {p}: {e}")
        # If we can't lock, fail-open (allow) to avoid blocking email entirely
        return True


def try_create_po_draft(
    archive_path: str | Path,
    po: Dict[str, Any],
    mailbox_upn: Optional[str] = None,
    to_recipients: Optional[List[str]] = None,
    cc_recipients: Optional[List[str]] = None,
    feature_flag_env: str = "EMAIL_DRAFT_ON_PO",
    lock_key: Optional[str] = None,
    lock_ttl_seconds: Optional[int] = None,
) -> Optional[dict]:
    """
    Creates an Outlook draft with the archived PDF attached.
    Safe to call from routes; failures are logged and won't raise.

    Idempotent: a per-PO single-flight lock prevents duplicate drafts within a short window.

    Returns the created draft (dict) or None on skip/failure/duplicate.
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

    # ---- Single-flight guard (prevents double drafts) ----
    # Use explicit lock_key if passed, else derive from project/po and the attachment name
    attachment_name = Path(str(archive_path)).name
    lk = lock_key or f"{project_number}__{po_num_str}__{attachment_name}"
    ttl = int(lock_ttl_seconds or DEFAULT_LOCK_TTL)

    if not _acquire_singleflight(lock_key=lk, ttl_seconds=ttl):
        logging.info(f"Email draft skipped due to fresh lock for key={lk}")
        return None
    # ------------------------------------------------------

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
