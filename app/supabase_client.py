import requests
import logging
from flask import current_app
from app.utils.status_utils import validate_po_status, POStatus
import string

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

def insert_po_bundle(data):
    """
    Inserts a new PO record and its associated metadata.
    - On creation: pass project_id, supplier_id, delivery info (and optionally po_number)
    - On edit: include original po_number and updated revision

    Returns the new PO UUID.
    """
    from flask import current_app
    import requests

    status = data.get("status", POStatus.DRAFT.value)
    revision = data.get("current_revision", "a")
    po_number = data.get("po_number")

    # print(f"Status ----------- : {data.get(status)}")

    validate_po_status(status)

    # Step 1: Insert into purchase_orders
    po_payload = {
        "project_id": data["project_id"],
        "supplier_id": data["supplier_id"],
        "status": status,
        "current_revision": revision,
        "delivery_contact_id": data.get("delivery_contact_id")
    }

    if po_number:
        po_payload["po_number"] = po_number  # Keep same number when editing

    # logging.info("📤 PO insert payload: %s", po_payload)

    po_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/purchase_orders"
    po_resp = requests.post(po_url, headers=get_headers(), json=po_payload)
    # logging.info("📬 PO response:", po_resp.status_code, po_resp.text)
    po_resp.raise_for_status()

    po_data = po_resp.json()
    po_id = po_data[0]["id"]

    # Step 2: Insert into po_metadata
    meta_payload = {
        "po_id": po_id,
        "delivery_terms": data["delivery_terms"],
        "delivery_date": data["delivery_date"],
        "supplier_contact_name": data.get("supplier_contact_name", ""),
        "supplier_reference_number": data.get("supplier_reference_number", ""),
        "test_certificates_required": data["test_certificates_required"],
        "active": True
    }

    # logging.info("📤 Metadata insert payload:", meta_payload)
    
    meta_url = f"{current_app.config['SUPABASE_URL']}/rest/v1/po_metadata"
    meta_resp = requests.post(meta_url, headers=get_headers(), json=meta_payload)
    # logging.info("📬 Metadata response:", meta_resp.status_code, meta_resp.text)
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

