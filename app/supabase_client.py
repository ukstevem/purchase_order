import requests
import logging
from flask import current_app
from app.utils.status_utils import validate_po_status, POStatus
import string
import uuid
from collections import defaultdict

# ------------------------------
# Supabase auth / headers
# ------------------------------

def _get_supabase_auth():
    """
    Returns (url, key) from Flask config.
    Prefers SECRET_KEY (service role) for server-side ops,
    then legacy/service/public keys as fallbacks.
    """
    url = current_app.config.get("SUPABASE_URL")
    key = (
        current_app.config.get("SECRET_KEY")  # service role (your current .env)
        or current_app.config.get("SUPABASE_SERVICE_ROLE_KEY")  # legacy
        or current_app.config.get("SUPABASE_KEY")               # publishable/anon
        or current_app.config.get("SUPABASE_ANON_KEY")          # optional alias
    )
    if not url:
        raise RuntimeError("Supabase misconfigured: SUPABASE_URL missing.")
    if not key:
        raise RuntimeError("Supabase misconfigured: set SECRET_KEY or a SUPABASE_* key.")
    return url, key


def get_headers(include_content_type=True):
    """
    Standard headers for Supabase REST calls.
    Uses the key chosen by _get_supabase_auth().
    """
    _, key = _get_supabase_auth()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Prefer": "return=representation",
    }
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


# ------------------------------
# Helpers
# ------------------------------

def _clean(x):
    x = (x or "").strip() if isinstance(x, str) else x
    return x or None

def _is_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except Exception:
        return False


# ------------------------------
# Fetchers
# ------------------------------

def fetch_suppliers():
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/suppliers"
    params = {
        "select": "*",
        "or": "(type.eq.supplier,type.eq.both)",
        "order": "name.asc"
    }
    r = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_projects():
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/projects"
    r = requests.get(url, headers=get_headers(False), params={"select": "*"}, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_delivery_addresses():
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/suppliers"
    params = {
        "select": "*",
        "or": "(type.eq.delivery,type.eq.both)",
        "order": "name.asc"
    }
    r = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_delivery_contacts():
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/delivery_contacts"
    params = {"select": "*", "order": "name.asc"}
    r = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# ------------------------------
# Delivery Contacts
# ------------------------------

def insert_delivery_contact(contact: dict) -> str:
    """
    Insert a delivery contact and return its UUID.

    Accepted keys:
      name (req), email (req), phone (opt), address_id (req, UUID), active (opt -> True)
    Also tolerates callers passing 'manual_contact_*' + 'delivery_address_id' and normalizes them.
    """
    # Backwards-compat: normalize manual_* payloads if present
    if any(k in contact for k in ("manual_contact_name", "manual_contact_email", "manual_contact_phone", "delivery_address_id")):
        contact = {
            "name":       contact.get("manual_contact_name"),
            "email":      contact.get("manual_contact_email"),
            "phone":      contact.get("manual_contact_phone"),
            "address_id": contact.get("delivery_address_id"),
            "active":     contact.get("active", True),
        }

    payload = {
        "name":       _clean(contact.get("name")),
        "email":      _clean(contact.get("email")),
        "phone":      _clean(contact.get("phone")),
        "address_id": contact.get("address_id"),
        "active":     bool(contact.get("active", True)),
    }

    # Validate required
    if not payload["name"] or not payload["email"]:
        raise ValueError("insert_delivery_contact requires non-empty 'name' and 'email'.")
    if not payload["address_id"] or not _is_uuid(payload["address_id"]):
        raise ValueError("insert_delivery_contact requires a valid UUID 'address_id' (Delivery Address dropdown).")

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/delivery_contacts"
    headers = get_headers()

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"body": resp.text}
        current_app.logger.error("❌ delivery_contacts insert failed %s: %s | payload=%s",
                                 resp.status_code, err, payload)
    resp.raise_for_status()

    data = resp.json()
    if not data or "id" not in data[0]:
        raise RuntimeError("delivery_contacts insert returned no id.")
    return data[0]["id"]


def _extract_manual_delivery_contact(data: dict) -> dict | None:
    """
    Pulls manual contact fields from metadata built in your routes.
    Matches your current names: manual_contact_name/phone/email.
    Returns None if nothing meaningful was provided.
    """
    name  = (data.get("manual_contact_name")  or "").strip()
    phone = (data.get("manual_contact_phone") or "").strip()
    email = (data.get("manual_contact_email") or "").strip()
    addr  = data.get("delivery_address_id")  # UUID string or None

    if not (name or phone or email):
        return None

    return {
        "name": name,
        "phone": phone,
        "email": email,
        "address_id": addr,
        "active": True,
    }


# ------------------------------
# PO insert / update
# ------------------------------

def insert_po_bundle(data):
    """
    Inserts a new PO record and its associated metadata.
    - If delivery_contact_id not provided but manual_contact_* present,
      create a delivery_contacts row and link it.
    Returns the new PO UUID.
    """
    status    = data.get("status", POStatus.DRAFT.value)
    revision  = data.get("current_revision", "a")
    po_number = data.get("po_number")

    validate_po_status(status)

    # Create contact from manual fields if needed
    delivery_contact_id = data.get("delivery_contact_id") or None
    if not delivery_contact_id:
        manual = _extract_manual_delivery_contact(data)
        if manual and manual.get("address_id"):
            try:
                delivery_contact_id = insert_delivery_contact(manual)
            except Exception as e:
                current_app.logger.error("❌ Manual delivery contact create failed: %s", e)
        else:
            current_app.logger.info("ℹ️ Skipping delivery_contacts insert (no manual or missing address_id).")

    # Step 1: purchase_orders
    po_payload = {
        "project_id": data["project_id"],
        "supplier_id": data["supplier_id"],
        "status": status,
        "current_revision": revision,
        "delivery_contact_id": delivery_contact_id,
    }
    if po_number:
        po_payload["po_number"] = po_number

    base, _ = _get_supabase_auth()
    po_url = f"{base}/rest/v1/purchase_orders"
    po_resp = requests.post(po_url, headers=get_headers(), json=po_payload, timeout=30)

    if po_resp.status_code >= 400:
        try:
            err = po_resp.json()
        except Exception:
            err = {"body": po_resp.text}
        current_app.logger.error("❌ purchase_orders insert failed %s: %s | payload=%s",
                                 po_resp.status_code, err, po_payload)
    po_resp.raise_for_status()

    po_id = po_resp.json()[0]["id"]

    # Step 2: po_metadata
    meta_payload = {
        "po_id": po_id,
        "delivery_terms": data["delivery_terms"],
        "delivery_date": data["delivery_date"],
        "supplier_contact_name": data.get("supplier_contact_name", ""),
        "supplier_reference_number": data.get("supplier_reference_number", ""),
        "test_certificates_required": data["test_certificates_required"],
        "active": True,
    }

    meta_url = f"{base}/rest/v1/po_metadata"
    meta_resp = requests.post(meta_url, headers=get_headers(), json=meta_payload, timeout=30)
    if meta_resp.status_code >= 400:
        try:
            err = meta_resp.json()
        except Exception:
            err = {"body": meta_resp.text}
        current_app.logger.error("❌ po_metadata insert failed %s: %s | payload=%s",
                                 meta_resp.status_code, err, meta_payload)
    meta_resp.raise_for_status()

    return po_id


def insert_line_items(items):
    if not items:
        return
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/po_line_items"
    resp = requests.post(url, headers=get_headers(), json=items, timeout=30)
    resp.raise_for_status()


def fetch_all_pos(project_id=None):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"
    params = {
        "select": "id,po_number,reference,status,current_revision,created_at,project_id,projects(projectnumber),suppliers(name)",
        "active": "eq.true"
    }
    if project_id:
        params["project_id"] = f"eq.{project_id}"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_active_pos(project_id=None):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/active_po_list"
    params = {"select": "*"}
    if project_id:
        params["project_id"] = f"eq.{project_id}"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_po_detail(po_id):
    print(f"🔍 Fetching PO {po_id}")
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    # Step 1: main PO + metadata
    po_url = f"{base}/rest/v1/purchase_orders"
    po_params = {
        "id": f"eq.{po_id}",
        "select": "*,projects(*),suppliers(*),po_metadata(*)",
        "po_metadata.active": "is.true"
    }
    po_resp = requests.get(po_url, headers=headers, params=po_params, timeout=30)
    po_resp.raise_for_status()

    try:
        po = po_resp.json()[0]
    except Exception as e:
        print(f"❌ JSON error: {e}")
        return None

    # Step 2: line items
    li_url = f"{base}/rest/v1/po_line_items"
    li_params = {"po_id": f"eq.{po_id}", "active": "is.true", "select": "*"}
    li_resp = requests.get(li_url, headers=headers, params=li_params, timeout=30)
    li_resp.raise_for_status()
    po["line_items"] = li_resp.json()

    # Step 3: delivery contact
    if po.get("delivery_contact_id"):
        dc_url = f"{base}/rest/v1/delivery_contacts"
        dc_params = {"id": f"eq.{po['delivery_contact_id']}", "select": "*"}
        dc_resp = requests.get(dc_url, headers=headers, params=dc_params, timeout=30)
        dc_resp.raise_for_status()
        dc_results = dc_resp.json()
        po["delivery_contact"] = dc_results[0] if dc_results else None

    # Step 4: delivery address (if not manual)
    if po.get("manual_delivery_address") is None:
        address_id = po.get("delivery_address_id")
        if not address_id and po.get("delivery_contact"):
            address_id = po["delivery_contact"].get("address_id")

        if address_id:
            da_url = f"{base}/rest/v1/suppliers"
            da_params = {"id": f"eq.{address_id}", "select": "*"}
            da_resp = requests.get(da_url, headers=headers, params=da_params, timeout=30)
            da_resp.raise_for_status()
            da_results = da_resp.json()
            po["delivery_address"] = da_results[0] if da_results else None

    return po


def deactivate_po_data(po_id):
    base, _ = _get_supabase_auth()
    meta_url = f"{base}/rest/v1/po_metadata"
    item_url = f"{base}/rest/v1/po_line_items"
    requests.patch(meta_url, headers=get_headers(), params={"po_id": f"eq.{po_id}"}, json={"active": False}, timeout=30)
    requests.patch(item_url, headers=get_headers(), params={"po_id": f"eq.{po_id}"}, json={"active": False}, timeout=30)


def insert_po_metadata(meta):
    meta["active"] = True
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/po_metadata"
    resp = requests.post(url, headers=get_headers(), json=meta, timeout=30)
    resp.raise_for_status()


def get_next_revision(po_id, status):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"
    params = {"id": f"eq.{po_id}", "select": "current_revision,status"}
    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    po = resp.json()[0]
    current = po.get("current_revision")
    current_status = po.get("status")

    # From draft to released
    if current_status != "released" and status == "released":
        return "1"

    # Still in draft
    if status == "draft":
        if not current:
            return "a"
        next_char = chr(ord(current) + 1)
        if next_char in string.ascii_lowercase:
            return next_char
        else:
            raise Exception("Too many draft revisions")

    # Already released, now increment numerically
    try:
        return str(int(current) + 1)
    except (ValueError, TypeError):
        return "1"


def fetch_all_po_revisions(po_number: int):
    """
    Fetch all revisions for a given PO number using Supabase REST API.
    """
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"
    params = {
        "po_number": f"eq.{po_number}",
        "select": "id,current_revision"
    }
    response = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_project_po_summary():
    """
    Build dashboard counts from active_po_list (already filtered to current/active rows).
    Treat anything not 'draft' as active (approved/released/issued/etc.).
    Returns: [{project: "P123", projectnumber: "P123", draft: 1, active: 2}, ...]
    """
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/active_po_list"
    params = {"select": "projectnumber,status"}
    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"body": resp.text}
        current_app.logger.error("❌ active_po_list fetch failed %s: %s", resp.status_code, err)
    resp.raise_for_status()

    rows = resp.json() or []
    agg = defaultdict(lambda: {"project": "", "projectnumber": "", "draft": 0, "active": 0})

    for r in rows:
        projectnumber = (r.get("projectnumber") or "—").strip()
        if not agg[projectnumber]["project"]:
            agg[projectnumber]["project"] = projectnumber
            agg[projectnumber]["projectnumber"] = projectnumber

        status = (r.get("status") or "").lower()
        if status == "draft":
            agg[projectnumber]["draft"] += 1
        else:
            agg[projectnumber]["active"] += 1

    return sorted(agg.values(), key=lambda x: x["projectnumber"])


# ------------------------------
# Accounts page helpers
# ------------------------------

def fetch_accounts_overview():
    """
    Read from the accounts_overview view (pre-aggregated totals).
    """
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/accounts_overview"
    headers = get_headers(False)
    params = {
        "select": "id,po_number,status,total_value,acc_complete,invoice_reference,projectnumber,supplier_name",
        "order": "po_number.asc",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if not resp.ok:
        current_app.logger.error("fetch_accounts_overview failed: %s", resp.text)
        return []
    return resp.json()



def update_po_accounts_fields(po_id: str, acc_complete=None, invoice_reference=None):
    """
    Patch a single PO's accounts fields. Only sends fields that are not None.
    """
    payload = {}
    if acc_complete is not None:
        payload["acc_complete"] = bool(acc_complete)
    if invoice_reference is not None:
        payload["invoice_reference"] = str(invoice_reference)

    if not payload:
        return {"ok": True, "count": 0, "data": None}

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"
    headers = get_headers()
    params = {"id": f"eq.{po_id}"}

    resp = requests.patch(url, headers=headers, params=params, json=payload, timeout=30)
    ok = resp.ok
    if not ok:
        current_app.logger.error("update_po_accounts_fields failed: %s", resp.text)
    data = resp.json() if ok and resp.text else None
    return {"ok": ok, "data": data, "status": resp.status_code, "text": resp.text}
