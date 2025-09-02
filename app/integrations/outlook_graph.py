# app/integrations/outlook_graph.py
import base64
import json
import os
import pathlib
import requests
from typing import List, Optional

import msal  # pip install msal

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _get_graph_token() -> str:
    """Client credentials flow (application permissions)."""
    tenant_id = os.environ["MS_TENANT_ID"]
    client_id = os.environ["MS_CLIENT_ID"]
    client_secret = os.environ["MS_CLIENT_SECRET"]

    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )

    result = app.acquire_token_silent(GRAPH_SCOPE, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(f"Failed to get Graph token: {result}")
    return result["access_token"]


def _graph_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_draft_with_attachment(
    mailbox_upn: str,
    subject: str,
    body_text: str,
    pdf_path: str,
    to_recipients: Optional[List[str]] = None,
    cc_recipients: Optional[List[str]] = None,
) -> dict:
    """
    Creates a DRAFT message in 'mailbox_upn' and attaches the given PDF.
    Returns the created message JSON.
    """
    token = _get_graph_token()

    def format_recipients(emails: Optional[List[str]]) -> List[dict]:
        if not emails:
            return []
        return [{"emailAddress": {"address": e}} for e in emails]

    # 1) Create an empty draft message (subject + body + recipients).
    create_url = f"{GRAPH_BASE}/users/{mailbox_upn}/messages"
    message_payload = {
        "subject": subject,
        "body": {
            "contentType": "Text",
            "content": body_text,
        },
        "toRecipients": format_recipients(to_recipients),
        "ccRecipients": format_recipients(cc_recipients),
        "importance": "Normal",
    }
    resp = requests.post(create_url, headers=_graph_headers(token), data=json.dumps(message_payload))
    if resp.status_code >= 300:
        raise RuntimeError(f"Create draft failed: {resp.status_code} {resp.text}")
    message = resp.json()
    message_id = message["id"]

    # 2) Attach the PDF (simple attachment – works up to ~3–10 MB reliably).
    path = pathlib.Path(pdf_path)
    with open(path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode("utf-8")

    attach_url = f"{GRAPH_BASE}/users/{mailbox_upn}/messages/{message_id}/attachments"
    attachment_payload = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": path.name,
        "contentType": "application/pdf",
        "contentBytes": pdf_b64,
    }
    aresp = requests.post(attach_url, headers=_graph_headers(token), data=json.dumps(attachment_payload))
    if aresp.status_code >= 300:
        raise RuntimeError(f"Attach failed: {aresp.status_code} {aresp.text}")

    # Return the final message (draft) object
    get_url = f"{GRAPH_BASE}/users/{mailbox_upn}/messages/{message_id}"
    final = requests.get(get_url, headers=_graph_headers(token))
    final.raise_for_status()
    return final.json()
