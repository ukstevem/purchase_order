import requests
import logging
from flask import current_app
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

def _headers_with_json(headers):
    return {**headers, "Content-Type": "application/json"}

def insert_delivery_contact(manual: dict) -> str:
    """
    Create a delivery_contacts row and return its UUID.
    Expected keys in `manual`: name, email, phone, address_id (uuid), org (optional)
    """
    base, headers = _get_supabase_auth()
    payload = {
        "name": manual.get("name", "").strip(),
        "email": manual.get("email", "").strip() or None,
        "phone": manual.get("phone", "").strip() or None,
        "address_id": manual.get("address_id"),  # must be a UUID
        "organisation": manual.get("org", "").strip() or None,
    }
    # Generate id client-side so we can use return=minimal if desired
    payload["id"] = str(uuid.uuid4())

    url = f"{base}/rest/v1/delivery_contacts"
    resp = requests.post(url, headers={**_headers_with_json(headers), "Prefer": "return=minimal"}, json=payload, timeout=30)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"body": resp.text}
        current_app.logger.error("❌ delivery_contacts insert failed %s: %s | payload=%s", resp.status_code, err, payload)
        resp.raise_for_status()

    return payload["id"]


# ------------------------------
# Helpers
# ------------------------------

def _resolve_projectnumber(base: str, headers: dict, proj: str | None) -> str | None:
    """
    If 'proj' looks like a legacy UUID, resolve its projectnumber from old 'projects'.
    Otherwise return 'proj' unchanged. Safe to call even if 'projects' will be retired soon.
    """
    if not proj:
        return None
    if not _is_uuid(proj):
        return proj  # already a projectnumber
    r = requests.get(
        f"{base}/rest/v1/projects",
        headers=headers,
        params={"select": "projectnumber", "id": f"eq.{proj}", "limit": 1},
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json() or []
    return rows[0]["projectnumber"] if rows else None

def fetch_project_item_options():
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/vw_project_item_options"
    r = requests.get(
        url,
        headers=get_headers(False),
        params={"select": "projectnumber,item_seq,line_desc,option_code,option_label",
                "order": "projectnumber.asc,item_seq.asc"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json() or []

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

import logging
import requests

# You already have:
# from app.supabase_client import _get_supabase_auth, get_headers


# ---- PROJECTS (project_register) ----

def fetch_projects():
    """
    Returns: [{"projectnumber": str, "projectdescription": str}, ...] sorted by projectnumber.
    `projectdescription` left blank because project_register has no description column.
    """
    proj_map = fetch_projects_map()
    return [
        {"projectnumber": pn, "projectdescription": data.get("projectdescription", "")}
        for pn, data in sorted(proj_map.items(), key=lambda kv: kv[0])
    ]


def fetch_projects_map():
    """
    Returns { projectnumber: {"projectnumber": str, "projectdescription": ""} }
    Single call to project_register; no probing of other tables/columns.
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    rel = "project_register"
    params = {"select": "projectnumber", "order": "projectnumber.asc", "limit": 10000}

    resp = requests.get(f"{base}/rest/v1/{rel}", headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logging.error("fetch_projects_map: %s failed (%s): %s", rel, resp.status_code, resp.text)
        resp.raise_for_status()

    rows = resp.json() or []
    return {
        str(r["projectnumber"]).strip(): {
            "projectnumber": str(r["projectnumber"]).strip(),
            "projectdescription": "",  # no description column in project_register
        }
        for r in rows
        if r.get("projectnumber") is not None
    }


# ---- SUPPLIERS (master table) ----

def fetch_suppliers(limit: int = 10000):
    """
    Returns a sorted list[str] of supplier names for dropdown hydration.
    Source: suppliers(name) — full master list (id, name, address, type).
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    rel = "suppliers"
    params = {"select": "name", "order": "name.asc", "limit": limit}

    resp = requests.get(f"{base}/rest/v1/{rel}", headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logging.warning("fetch_suppliers: %s failed (%s): %s", rel, resp.status_code, resp.text)
        resp.raise_for_status()

    rows = resp.json() or []
    names = {
        str(r.get("name")).strip()
        for r in rows
        if r.get("name")
    }
    return sorted(names)
    
# routes.py
def suppliers_as_objects():
    """
    Normalize suppliers to [{'id': ..., 'name': ...}, ...] using the current
    fetch_suppliers() output (which may be list[str] or list[dict]).
    """
    rows = fetch_suppliers() or []
    # Already correct?
    if rows and isinstance(rows[0], dict) and "id" in rows[0] and "name" in rows[0]:
        # Sort for nice UX
        return sorted(rows, key=lambda s: (s.get("name") or "").lower())

    # Otherwise rows is likely list[str] of names – hydrate ids from Supabase.
    base, _ = _get_supabase_auth()
    hdr = get_headers(False)
    import requests

    try:
        r = requests.get(
            f"{base}/rest/v1/suppliers",
            headers=hdr,
            params={"select": "id,name", "order": "name.asc", "limit": 10000},
            timeout=30,
        )
        r.raise_for_status()
        all_sup = r.json() or []
    except Exception:
        all_sup = []

    name_to_id = { (s.get("name") or "").strip(): s.get("id") for s in all_sup }
    # Build normalized objects; id may be None if a name isn’t found.
    out = []
    for n in rows:
        if not n: 
            continue
        n_clean = str(n).strip()
        out.append({"id": name_to_id.get(n_clean), "name": n_clean})
    return out

# ---- OPTIONAL: Suppliers from the active view (if you prefer only names that appear in POs) ----
def fetch_suppliers_from_view(limit: int = 10000):
    """
    Returns a sorted list[str] of supplier names that currently appear in active_po_list.
    Source: active_po_list(supplier_name).
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    rel = "active_po_list"
    params = {"select": "supplier_name", "order": "supplier_name.asc", "limit": limit}

    resp = requests.get(f"{base}/rest/v1/{rel}", headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logging.warning("fetch_suppliers_from_view: %s failed (%s): %s", rel, resp.status_code, resp.text)
        resp.raise_for_status()

    rows = resp.json() or []
    names = {
        str(r.get("supplier_name")).strip()
        for r in rows
        if r.get("supplier_name")
    }
    return sorted(names)


def fetch_projects():
    """
    Returns: [{"projectnumber": str, "projectdescription": str}, ...] sorted by projectnumber.
    `projectdescription` left blank because project_register has no description column.
    """
    proj_map = fetch_projects_map()
    return [
        {"projectnumber": pn, "projectdescription": data.get("projectdescription", "")}
        for pn, data in sorted(proj_map.items(), key=lambda kv: kv[0])
    ]


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

# --- Spend report helpers ---

def fetch_last_issued_dates(po_numbers: list[str], first_month_start_iso: str, next_month_start_iso: str):
    """
    For the given po_numbers, fetch rows where status='issued' and updated_at is within the window,
    ordered so the FIRST seen per po_number is the *latest* issued row in that window.
    Returns dict { po_number: updated_at_iso }.
    """
    if not po_numbers:
        return {}

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    # Build in.(...) list with no spaces
    in_list = ",".join(po_numbers)

    gte = f"{first_month_start_iso}T00:00:00Z"
    lt  = f"{next_month_start_iso}T00:00:00Z"

    params = [
        ("select", "po_number,updated_at"),
        ("po_number", f"in.({in_list})"),
        ("status", "eq.issued"),
        ("updated_at", f"gte.{gte}"),
        ("updated_at", f"lt.{lt}"),
        # Order by po_number, then latest updated_at first so we can take first per key
        ("order", "po_number.asc,updated_at.desc"),
        ("limit", "100000"),
    ]

    r = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    if r.status_code >= 400:
        current_app.logger.error("❌ fetch_last_issued_dates: %s", r.text)
    r.raise_for_status()
    rows = r.json() or []

    latest_issued = {}
    for row in rows:
        pn = row.get("po_number")
        if pn and pn not in latest_issued:
            latest_issued[pn] = row.get("updated_at")  # ISO timestamp
    return latest_issued

# def fetch_projects_map():
#     """
#     Returns { project_id(uuid): {"projectnumber": str, "projectdescription": str} }
#     """
#     base, _ = _get_supabase_auth()
#     url = f"{base}/rest/v1/projects"
#     params = {
#         "select": "id,projectnumber,projectdescription",
#         "order": "projectnumber.asc",
#         "limit": 10000,
#     }
#     resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
#     resp.raise_for_status()
#     rows = resp.json() or []
#     return {r["id"]: {"projectnumber": r["projectnumber"], "projectdescription": r["projectdescription"]} for r in rows}

def fetch_projects_map():
    """
    Returns { projectnumber: {"projectnumber": str, "projectdescription": ""} }
    Single call to project_register; no probing of other tables/columns.
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    rel = "project_register"
    params = {"select": "projectnumber", "order": "projectnumber.asc", "limit": 10000}

    resp = requests.get(f"{base}/rest/v1/{rel}", headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logging.error("fetch_projects_map: %s failed (%s): %s", rel, resp.status_code, resp.text)
        resp.raise_for_status()

    rows = resp.json() or []
    return {
        str(r["projectnumber"]).strip(): {
            "projectnumber": str(r["projectnumber"]).strip(),
            "projectdescription": "",  # no description column in project_register
        }
        for r in rows
        if r.get("projectnumber") is not None
    }


def fetch_suppliers(limit: int = 10000):
    """
    Returns a sorted list[str] of supplier names for dropdown hydration.
    Source: suppliers(name) — full master list (id, name, address, type).
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    rel = "suppliers"
    params = {"select": "name", "order": "name.asc", "limit": limit}

    resp = requests.get(f"{base}/rest/v1/{rel}", headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        logging.warning("fetch_suppliers: %s failed (%s): %s", rel, resp.status_code, resp.text)
        resp.raise_for_status()

    rows = resp.json() or []
    names = {
        str(r.get("name")).strip()
        for r in rows
        if r.get("name")
    }
    return sorted(names)



def fetch_purchase_orders_since(first_month_start_iso: str, next_month_start_iso: str):
    """
    Fetch POs within [first_month_start_iso, next_month_start_iso) for statuses approved/issued/complete.
    Returns rows ordered so the first row per po_number is the latest revision.
    """
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    # Use a list of (key, value) tuples so we can repeat 'updated_at' twice.
    params = [
        ("select", "id,project_id,po_number,updated_at,total_value,status"),
        ("status", "in.(approved,issued,complete)"),
        ("updated_at", f"gte.{first_month_start_iso}"),
        ("updated_at", f"lt.{next_month_start_iso}"),
        ("order", "po_number.asc,updated_at.desc"),
        ("limit", "100000"),
    ]

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json() or []

# --- Spend report (via accounts_overview) ---

def fetch_pos_from_po_table(project_id=None, date_from=None, date_to=None,
                            statuses=None, order_by="updated_at.desc"):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    params = {
        # embed suppliers only; derive projectnumber from project_id directly
        "select": "id,po_number,reference,status,current_revision,project_id,updated_at,suppliers(name)"
    }
    if order_by:
        params["order"] = order_by

    parts = []
    if project_id:
        headers = get_headers(False)
        pn = _resolve_projectnumber(base, headers, str(project_id))
        if pn:
            parts.append(f"project_id.eq.{pn}")
    if date_from:
        parts.append(f"updated_at.gte.{date_from}T00:00:00")
    if date_to:
        parts.append(f"updated_at.lt.{date_to}T00:00:00")
    if statuses:
        parts.append(f"status.in.({','.join(statuses)})")
    if parts:
        params["and"] = f"({','.join(parts)})"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json() or []

    for r in rows:
        # expose projectnumber + supplier_name for templates expecting them
        r["projectnumber"] = r.get("project_id")
        supp = r.pop("suppliers", None)
        if isinstance(supp, dict):
            r["supplier_name"] = supp.get("name")
    return rows


def fetch_accounts_overview_latest(statuses=("approved", "issued", "complete")):
    """
    Get latest-only PO rows from the accounts_overview view with total_value pre-aggregated.
    Returns [{id, po_number, status, projectnumber, supplier_name, total_value, acc_complete, invoice_reference}]
    """
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/accounts_overview"
    status_list = ",".join(statuses)
    params = [
        ("select", "id,po_number,status,projectnumber,supplier_name,total_value"),
        ("status", f"in.({status_list})"),
        ("limit", "100000"),
        ("order", "po_number.asc"),  # stable ordering
    ]
    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    if resp.status_code >= 400:
        current_app.logger.error("❌ fetch_accounts_overview_latest: %s", resp.text)
    resp.raise_for_status()
    return resp.json() or []


def fetch_po_updated_at_for_ids_in_window(ids: list[str], first_month_start_iso: str, next_month_start_iso: str):
    """
    For a set of PO ids, fetch updated_at within [first_month_start, next_month_start).
    Returns [{id, updated_at}] for those that fall inside the window.
    Uses RFC3339 timestamps to satisfy timestamptz comparisons.
    """
    if not ids:
        return []

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    # Build id IN (...) list (comma-separated, no spaces)
    in_list = ",".join(ids)

    gte = f"{first_month_start_iso}T00:00:00Z"
    lt  = f"{next_month_start_iso}T00:00:00Z"

    params = [
        ("select", "id,updated_at"),
        ("id", f"in.({in_list})"),
        ("updated_at", f"gte.{gte}"),
        ("updated_at", f"lt.{lt}"),
        ("limit", "100000"),
        ("order", "updated_at.asc"),
    ]

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    if resp.status_code >= 400:
        current_app.logger.error("❌ fetch_po_updated_at_for_ids_in_window: %s", resp.text)
    resp.raise_for_status()
    return resp.json() or []

def fetch_active_pos_from_view(projectnumber=None, supplier_name=None, status=None, date_from=None, date_to=None,
                               order_by="updated_at.desc"):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/active_po_list"

    params = {
        "select": "*",
    }

    filters = []

    if projectnumber:
        filters.append(f"projectnumber.ilike.%{projectnumber}%")
    if supplier_name:
        params["supplier_name"] = f"eq.{supplier_name}"
    if status:
        params["status"] = f"eq.{status}"
    if date_from:
        filters.append(f"updated_at.gte.{date_from}T00:00:00")
    if date_to:
        filters.append(f"updated_at.lt.{date_to}T00:00:00")

    if filters:
        params["and"] = f"({','.join(filters)})"

    if order_by:
        params["order"] = order_by

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json() or []


def fetch_pos_latest_from_po_table(project_id=None, date_from=None, date_to=None,
                                   statuses=None, order_by="updated_at.desc"):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    params = {
        "select": (
            "id,po_number,reference,status,current_revision,project_id,updated_at,"
            "suppliers(name),po_metadata!inner(id)"
        ),
        "po_metadata.active": "is.true",
    }
    if order_by:
        params["order"] = order_by

    parts = []
    if project_id:
        headers = get_headers(False)
        pn = _resolve_projectnumber(base, headers, str(project_id))
        if pn:
            parts.append(f"project_id.eq.{pn}")
    if date_from:
        parts.append(f"updated_at.gte.{date_from}T00:00:00")
    if date_to:
        parts.append(f"updated_at.lt.{date_to}T00:00:00")
    if statuses:
        parts.append(f"status.in.({','.join(statuses)})")
    if parts:
        params["and"] = f"({','.join(parts)})"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json() or []

    for r in rows:
        r["projectnumber"] = r.get("project_id")
        supp = r.pop("suppliers", None)
        if isinstance(supp, dict):
            r["supplier_name"] = supp.get("name")
    return rows

# ------------------------------
# Delivery Contacts
# ------------------------------

def fetch_last_issued_dates_any(po_numbers: list[str]) -> dict[str, str]:
    """
    For the given po_numbers, fetch ALL rows where status='issued',
    ordered so the FIRST seen per po_number is the latest issued row overall.
    Returns dict { po_number: updated_at_iso }.
    """
    if not po_numbers:
        return {}

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"

    in_list = ",".join(str(p) for p in po_numbers)  # comma separated, no spaces

    params = [
        ("select", "po_number,updated_at"),
        ("po_number", f"in.({in_list})"),
        ("status", "eq.issued"),
        ("order", "po_number.asc,updated_at.desc"),
        ("limit", "100000"),
    ]

    r = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    if r.status_code >= 400:
        current_app.logger.error("❌ fetch_last_issued_dates_any: %s", r.text)
    r.raise_for_status()
    rows = r.json() or []

    latest_issued = {}
    for row in rows:
        pn = str(row.get("po_number"))
        if pn and pn not in latest_issued:
            latest_issued[pn] = row.get("updated_at")
    return latest_issued


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
    - If delivery_contact_id not provided but manual_contact_* present, create a delivery_contacts row and link it.
    Returns the new PO UUID.
    """
    status    = data.get("status", "draft")
    revision  = data.get("current_revision", "a")
    po_number = data.get("po_number")

    # Create contact from manual fields if needed
    delivery_contact_id = data.get("delivery_contact_id") or None
    if not delivery_contact_id:
        manual = _extract_manual_delivery_contact(data)  # assumes you already have this util
        if manual and manual.get("address_id"):
            try:
                delivery_contact_id = insert_delivery_contact(manual)
            except Exception as e:
                current_app.logger.error("❌ Manual delivery contact create failed: %s", e)
        else:
            current_app.logger.info("ℹ️ Skipping delivery_contacts insert (no manual or missing address_id).")

    # ---- Step 1: purchase_orders (request only the id back) ----
    po_payload = {
        "project_id": data["project_id"],                         # TEXT (project number)
        "item_seq": data["item_seq"],
        "supplier_id": data["supplier_id"] or None,               # UUID or None (not "")
        "status": status,
        "current_revision": revision,
        "delivery_contact_id": delivery_contact_id or None,       # UUID or None
    }
    if po_number:
        po_payload["po_number"] = po_number

    base, _ = _get_supabase_auth()
    po_url = f"{base}/rest/v1/purchase_orders?select=id"          # <-- only return id
    po_resp = requests.post(
        po_url,
        headers=get_headers(),
        json=po_payload,
        timeout=30
    )

    if po_resp.status_code >= 400:
        try:
            err = po_resp.json()
        except Exception:
            err = {"body": po_resp.text}
        current_app.logger.error("❌ purchase_orders insert failed %s: %s | payload=%s",
                                 po_resp.status_code, err, po_payload)
    po_resp.raise_for_status()

    po_id = po_resp.json()[0]["id"]

    # ---- Step 2: po_metadata ----
    meta_payload = {
        "po_id": po_id,
        "delivery_terms": data.get("delivery_terms", ""),
        "delivery_date": data.get("delivery_date"),  # allow null
        "supplier_contact_name": data.get("supplier_contact_name", ""),
        "supplier_reference_number": data.get("supplier_reference_number", ""),
        "test_certificates_required": bool(data.get("test_certificates_required", False)),
        "active": True,
    }

    meta_url = f"{base}/rest/v1/po_metadata"
    meta_resp = requests.post(
        meta_url,
        headers=get_headers(),
        json=meta_payload,
        timeout=30
    )
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
        # no project_register embed here (avoid 300 ambiguity)
        "select": "id,po_number,reference,status,current_revision,created_at,project_id,suppliers(name)",
        "active": "eq.true",
        "order": "po_number.asc",
    }
    if project_id:
        # accept either legacy UUID or projectnumber
        headers = get_headers(False)
        pn = _resolve_projectnumber(base, headers, str(project_id))
        if pn:
            params["project_id"] = f"eq.{pn}"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json() or []

    # flatten: expose 'projectnumber' and 'supplier_name' like before
    for r in rows:
        r["projectnumber"] = r.get("project_id")
        supp = r.pop("suppliers", None)
        if isinstance(supp, dict):
            r["supplier_name"] = supp.get("name")
    return rows



def fetch_active_pos(project_id=None, date_from=None, date_to=None, order_by="updated_at.desc"):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/active_po_list"

    params = {"select": "*"}
    if order_by:
        params["order"] = order_by

    parts = []
    if project_id:
        headers = get_headers(False)
        pn = _resolve_projectnumber(base, headers, str(project_id))
        if pn:
            parts.append(f"projectnumber.eq.{pn}")  # view typically has 'projectnumber'
    if date_from:
        parts.append(f"updated_at.gte.{date_from}T00:00:00")
    if date_to:
        parts.append(f"updated_at.lt.{date_to}T00:00:00")

    if parts:
        params["and"] = f"({','.join(parts)})"

    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def fetch_po_detail(po_id):
    print(f"🔍 Fetching PO {po_id}")
    base, _ = _get_supabase_auth()
    headers = get_headers(False)

    # Step 1: PO + metadata + supplier (no project_register embed)
    po_url = f"{base}/rest/v1/purchase_orders"
    po_params = {
        "id": f"eq.{po_id}",
        "select": "*,suppliers(*),po_metadata(*)",
        "po_metadata.active": "is.true"
    }
    po_resp = requests.get(po_url, headers=headers, params=po_params, timeout=30)
    po_resp.raise_for_status()

    try:
        po = po_resp.json()[0]
    except Exception as e:
        print(f"❌ JSON error: {e}")
        return None

    # Derive 'projectnumber' from project_id
    po["projectnumber"] = po.get("project_id")

    # Step 1b: (Optional) fetch project_register row if you need extra fields (e.g., client_id)
    if po.get("projectnumber"):
        pr_url = f"{base}/rest/v1/project_register"
        pr_params = {"projectnumber": f"eq.{po['projectnumber']}", "select": "*", "limit": "1"}
        pr_resp = requests.get(pr_url, headers=headers, params=pr_params, timeout=15)
        if pr_resp.ok:
            pr_rows = pr_resp.json() or []
            po["project_register"] = pr_rows[0] if pr_rows else None

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


import string
from datetime import datetime
import requests

def get_next_revision(po_id, status):
    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/purchase_orders"
    params = {"id": f"eq.{po_id}", "select": "current_revision,status"}
    resp = requests.get(url, headers=get_headers(False), params=params, timeout=30)
    resp.raise_for_status()
    po = resp.json()[0]
    current = po.get("current_revision")
    current_status = po.get("status")

    next_rev = None
    update_release_time = False

    # From draft to released
    if current_status != "released" and status == "released":
        next_rev = "1"
        update_release_time = True

    # Still in draft — bump alphabetically
    elif status == "draft":
        if not current:
            next_rev = "a"
        else:
            next_char = chr(ord(current) + 1)
            if next_char in string.ascii_lowercase:
                next_rev = next_char
            else:
                raise Exception("Too many draft revisions")

    # Already released — bump numerically
    else:
        try:
            next_rev = str(int(current) + 1)
            update_release_time = True
        except (ValueError, TypeError):
            next_rev = "1"
            update_release_time = True

    # --- Update last_release timestamp if needed ---
    if update_release_time:
        patch_url = f"{base}/rest/v1/purchase_orders"
        patch_params = {"id": f"eq.{po_id}"}
        now = datetime.utcnow().isoformat()
        patch_data = {"last_release": now}
        requests.patch(
            patch_url, headers=get_headers(), params=patch_params, json=patch_data, timeout=30
        )

    return next_rev



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
