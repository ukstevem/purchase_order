import requests
import logging
from flask import current_app
from app.utils.status_utils import validate_po_status, POStatus
import string
import uuid

def get_headers():
    key = current_app.config.get("SUPABASE_API_KEY")
    if not key:
        raise ValueError("SUPABASE_API_KEY missing from app config")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def fetch_suppliers():
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/suppliers"
    params = {
        "select": "*",
        "or": "(type.eq.supplier,type.eq.both)",
        "order": "name.asc"
    }
    r = requests.get(url, headers=get_headers(), params=params)
    r.raise_for_status()
    return r.json()

def fetch_projects():
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/projects"
    r = requests.get(url, headers=get_headers(), params={"select": "*"})
    r.raise_for_status()
    return r.json()

def fetch_delivery_addresses():
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/suppliers"
    params = {
        "select": "*",
        "or": "(type.eq.delivery,type.eq.both)",
        "order": "name.asc"
    }
    r = requests.get(url, headers=get_headers(), params=params)
    r.raise_for_status()
    return r.json()

def fetch_delivery_contacts():
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/delivery_contacts"
    params = {
        "select": "*",
        "order": "name.asc"
    }
    r = requests.get(url, headers=get_headers(), params=params)
    r.raise_for_status()
    return r.json()

def _clean(x):
    x = (x or "").strip() if isinstance(x, str) else x
    return x or None

def _is_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except Exception:
        return False

def insert_delivery_contact(contact: dict) -> str:
    """
    Insert a delivery contact and return its UUID.

    Accepted keys:
      name (req), email (req), phone (opt), address_id (req, UUID), active (opt -> True)
    Also tolerates callers passing 'manual_contact_*' + 'delivery_address_id' and normalizes them.
    """
    # 🔧 Backwards-compat: normalize manual_* payloads from routes if present
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

    # ✅ Validate required fields
    if not payload["name"] or not payload["email"]:
        raise ValueError("insert_delivery_contact requires non-empty 'name' and 'email'.")
    if not payload["address_id"] or not _is_uuid(payload["address_id"]):
        raise ValueError("insert_delivery_contact requires a valid UUID 'address_id' (Delivery Address dropdown).")

    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/delivery_contacts"
    headers = {**get_headers(), "Prefer": "return=representation"}

    resp = requests.post(url, headers=headers, json=payload)

    # Helpful error logging if Supabase rejects the row
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

    # If all empty, treat as absent
    if not (name or phone or email):
        return None

    # You can extend with company/address if you add those fields later
    return {
        "name": name,
        "phone": phone,
        "email": email,
        "address_id": addr,   # <-- crucial for insert_delivery_contact
        "active": True,
    }

def insert_po_bundle(data):
    """
    Inserts a new PO record and its associated metadata.
    - If delivery_contact_id not provided but manual_contact_* present,
      create a delivery_contacts row and link it.
    Returns the new PO UUID.
    """
    from flask import current_app
    import requests

    status    = data.get("status", POStatus.DRAFT.value)
    revision  = data.get("current_revision", "a")
    po_number = data.get("po_number")

    validate_po_status(status)

    # === Create contact from manual fields if needed (now includes address_id via extractor) ===
    delivery_contact_id = data.get("delivery_contact_id") or None
    if not delivery_contact_id:
        manual = _extract_manual_delivery_contact(data)  # includes address_id mapped from delivery_address_id
        if manual and manual.get("address_id"):
            try:
                delivery_contact_id = insert_delivery_contact(manual)
            except Exception as e:
                current_app.logger.error("❌ Manual delivery contact create failed: %s", e)
        else:
            current_app.logger.info("ℹ️ Skipping delivery_contacts insert (no manual or missing address_id).")

    # === Step 1: Insert into purchase_orders (MINIMAL payload – avoid unknown columns) ===
    po_payload = {
        "project_id": data["project_id"],
        "supplier_id": data["supplier_id"],
        "status": status,
        "current_revision": revision,
        "delivery_contact_id": delivery_contact_id,  # may be None or a UUID
    }
    if po_number:
        po_payload["po_number"] = po_number  # Keep same number when editing

    po_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    po_resp = requests.post(po_url, headers=get_headers(), json=po_payload)

    if po_resp.status_code >= 400:
        # surface real error to logs so we can see what's wrong
        try:
            err = po_resp.json()
        except Exception:
            err = {"body": po_resp.text}
        current_app.logger.error("❌ purchase_orders insert failed %s: %s | payload=%s",
                                 po_resp.status_code, err, po_payload)
    po_resp.raise_for_status()

    po_id = po_resp.json()[0]["id"]

    # === Step 2: Insert into po_metadata (store manual fields for display/PDF) ===
    meta_payload = {
        "po_id": po_id,
        "delivery_terms": data["delivery_terms"],
        "delivery_date": data["delivery_date"],
        "supplier_contact_name": data.get("supplier_contact_name", ""),
        "supplier_reference_number": data.get("supplier_reference_number", ""),
        "test_certificates_required": data["test_certificates_required"],
        "active": True,
    }

    meta_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_metadata"
    meta_resp = requests.post(meta_url, headers=get_headers(), json=meta_payload)
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
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_line_items"
    resp = requests.post(url, headers=get_headers(), json=items)
    resp.raise_for_status()

def fetch_all_pos(project_id=None):
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    params = {
        "select": "id,po_number,reference,status,current_revision,created_at,project_id,projects(projectnumber),suppliers(name)",
        "active": "eq.true"
    }

    if project_id:
        params["project_id"] = f"eq.{project_id}"

    resp = requests.get(url, headers=get_headers(), params=params)
    resp.raise_for_status()
    return resp.json()

def fetch_active_pos(project_id=None):
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/active_po_list"
    params = {
        "select": "*"
    }

    if project_id:
        params["project_id"] = f"eq.{project_id}"

    resp = requests.get(url, headers=get_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_po_detail(po_id):
    print(f"🔍 Fetching PO {po_id}")

    # Step 1: fetch main PO + metadata
    po_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    po_params = {
        "id": f"eq.{po_id}",
        "select": "*,projects(*),suppliers(*),po_metadata(*)",
        "po_metadata.active": "is.true"
    }

    headers = get_headers()
    po_resp = requests.get(po_url, headers=headers, params=po_params)
    po_resp.raise_for_status()

    try:
        po = po_resp.json()[0]
    except Exception as e:
        print(f"❌ JSON error: {e}")
        return None

    # Step 2: fetch line items
    li_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_line_items"
    li_params = {"po_id": f"eq.{po_id}", "active": "is.true", "select": "*"}
    li_resp = requests.get(li_url, headers=headers, params=li_params)
    li_resp.raise_for_status()
    po["line_items"] = li_resp.json()

    # Step 3: fetch delivery contact (if present)
    if po.get("delivery_contact_id"):
        dc_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/delivery_contacts"
        dc_params = {
            "id": f"eq.{po['delivery_contact_id']}",
            "select": "*"
        }
        dc_resp = requests.get(dc_url, headers=headers, params=dc_params)
        dc_resp.raise_for_status()
        dc_results = dc_resp.json()
        po["delivery_contact"] = dc_results[0] if dc_results else None

    # Step 4: fetch delivery address (if not manual)
    if po.get("manual_delivery_address") is None:
        address_id = po.get("delivery_address_id")

        # Fallback: use contact.address_id if direct one is missing
        if not address_id and po.get("delivery_contact"):
            address_id = po["delivery_contact"].get("address_id")

        if address_id:
            da_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/suppliers"
            da_params = {
                "id": f"eq.{address_id}",
                "select": "*"
            }
            da_resp = requests.get(da_url, headers=headers, params=da_params)
            da_resp.raise_for_status()
            da_results = da_resp.json()
            po["delivery_address"] = da_results[0] if da_results else None

    return po


def deactivate_po_data(po_id):
    meta_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_metadata"
    item_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_line_items"

    requests.patch(meta_url, headers=get_headers(), params={"po_id": f"eq.{po_id}"}, json={"active": False})
    requests.patch(item_url, headers=get_headers(), params={"po_id": f"eq.{po_id}"}, json={"active": False})

def insert_po_metadata(meta):
    meta["active"] = True
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_metadata"
    resp = requests.post(url, headers=get_headers(), json=meta)
    resp.raise_for_status()

def get_next_revision(po_id, status):
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    params = {"id": f"eq.{po_id}", "select": "current_revision,status"}
    resp = requests.get(url, headers=get_headers(), params=params)
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
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    params = {
        "po_number": f"eq.{po_number}",
        "select": "id,current_revision"
    }

    response = requests.get(url, headers=get_headers(), params=params)
    response.raise_for_status()
    return response.json()
