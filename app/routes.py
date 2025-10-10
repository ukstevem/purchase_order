from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response, jsonify, current_app
from app.supabase_client import (
    fetch_suppliers, 
    suppliers_as_objects,
    fetch_delivery_addresses,
    fetch_po_detail,
    fetch_projects, 
    insert_po_bundle, 
    insert_line_items, 
    fetch_delivery_contacts,
    fetch_pos_latest_from_po_table,
    fetch_active_pos_from_view,
    fetch_project_po_summary,
    fetch_last_issued_dates_any,
    fetch_accounts_overview_latest,
    _get_supabase_auth, 
    get_headers,
    deactivate_po_data,
    )
from app.utils.forms import parse_po_form
from .utils.revision import get_next_revision, compute_updated_revision
from app.utils.status_utils import (
    allowed_next_statuses, 
    is_forward_or_same, 
    coerce_rev_on_leaving_draft,
    validate_po_status
    )
from app.utils.pdf_archive import save_pdf_archive
from weasyprint import HTML, CSS
from datetime import datetime, date
from flask import current_app, render_template, request, session, flash
from .utils.certs_table import load_certs_table
from werkzeug.utils import secure_filename
import base64, uuid, requests
from pathlib import Path
from app.integrations.outlook_graph import create_draft_with_attachment
from app.services.po_email import try_create_po_draft
from zoneinfo import ZoneInfo
import re

main = Blueprint("main", __name__)

# boolean and string normalisation helper

def _f(name, default=None):
    return (request.form.get(name) or default)

def _f_bool(name):
    # checkboxes come as "on" or missing
    return request.form.get(name) in ("on", "true", "True", "1")

def _active_po_metadata(po) -> dict:
    """Return a dict for the active metadata, or {} if missing."""
    pm = po.get("po_metadata")
    if not pm:
        return {}
    if isinstance(pm, dict):
        return pm
    if isinstance(pm, list):
        # prefer the active row; otherwise last one
        return next((m for m in pm if m.get("active")), (pm[-1] if pm else {}))
    return {}

@main.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404

@main.route("/")
def index():
    try:
        summary = fetch_project_po_summary()
    except Exception as e:
        flash(f"Failed to load dashboard: {e}", "danger")
        summary = []
    return render_template("index.html", summary=summary)

@main.route("/home")
def home_redirect():
    return redirect(url_for("main.index"))

# app/blueprints/main.py (or wherever po_list lives)
# from flask import request, render_template, flash
# from app.supabase_client import (
#     fetch_active_pos_from_view,  # extend this helper as shown below
#     fetch_projects,              # already used by Accounts
#     fetch_suppliers,             # already used by Accounts
# )

@main.route("/po-list")
def po_list():
    from flask import request, render_template, flash

    # Existing date + sorting params (kept)
    date_from = request.args.get("from")
    date_to   = request.args.get("to")
    sort      = request.args.get("sort", "po_number")
    dir_      = (request.args.get("dir", "desc") or "desc").lower()

    allowed_sorts = {"po_number", "updated_at"}
    if sort not in allowed_sorts:
        sort = "po_number"
    dir_ = "asc" if dir_ == "asc" else "desc"
    order_by = f"{sort}.{dir_}"

    # Filters
    selected_status   = (request.args.get("status", "") or "").strip().lower()
    selected_project  = (request.args.get("project", "") or "").strip()
    selected_supplier = (request.args.get("supplier", "") or "").strip()

    try:
        # Hydrate dropdowns
        projects_rows = fetch_projects() or []   # [{projectnumber, projectdescription}, ...]
        project_options = sorted({
            (row.get("projectnumber") or "").strip()
            for row in projects_rows
            if row and row.get("projectnumber")
        })

        supplier_options = sorted({
            (s or "").strip()
            for s in (fetch_suppliers() or [])   # fetch_suppliers already returns list[str]
            if s
        })

        # Fetch the list with filters applied server-side
        pos = fetch_active_pos_from_view(
            projectnumber=selected_project or None,
            supplier_name=selected_supplier or None,
            status=selected_status or None,
            date_from=date_from,
            date_to=date_to,
            order_by=order_by,
        ) or []

    except Exception as e:
        flash(f"Failed to load POs: {e}", "danger")
        pos = []
        project_options, supplier_options = [], []

    return render_template(
        "po_list.html",
        pos=pos,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        dir=dir_,
        selected_status=selected_status,
        selected_project=selected_project,
        selected_supplier=selected_supplier,
        project_options=project_options,
        supplier_options=supplier_options,
    )




@main.route("/po/<po_id>")
def po_preview(po_id):
    from .supabase_client import fetch_po_detail
    try:
        po = fetch_po_detail(po_id)
        md_list = po.get("po_metadata") or []
        md = md_list[0] if isinstance(md_list, list) and md_list else {}
        if not po:
            return render_template("404.html"), 404
    except Exception as e:
        flash(f"Failed to load PO: {e}", "danger")
        return redirect(url_for("main.po_list"))

    # Compute line totals and footer total
    grand_total = 0
    net_total = 0
    vat_total = 0
    for item in po.get("line_items", []):
        qty = item.get("quantity") or 0
        price = item.get("unit_price") or 0.0
        item["total"] = qty * price
        net_total += item["total"]
        vat_total = net_total * 0.2
        grand_total = net_total + vat_total

    return render_template("po_web.html", po=po, md=md, grand_total=grand_total, vat_total=vat_total, net_total=net_total, now=datetime.now())

@main.route("/create-po", methods=["GET", "POST"])
def create_po():
    if request.method == "POST":
        try:
            # 🔒 Idempotency
            idem = request.form.get("idempotency_key")
            if not idem or idem != session.pop("last_form_token", None):
                flash("This form was already submitted or the token is invalid.", "warning")
                return redirect(url_for("main.create_po"))

            metadata, line_items = parse_po_form(request.form)

            # ✅ Your schema: purchase_orders.project_id (TEXT PN) + item_seq
            metadata["project_id"] = (request.form.get("project_id") or "").strip()   # PN goes here
            metadata["item_seq"]   = (request.form.get("item_seq") or "").strip()

            if not metadata["project_id"] or metadata["item_seq"] == "":
                flash("Please select a Project / Item.", "danger")
                return redirect(url_for("main.create_po"))

            metadata["test_certificates_required"] = _f_bool("test_cert_required")
            metadata["status"] = "draft"
            metadata["current_revision"] = "a"

            # 🚫 No manual delivery address allowed
            manual_address_text = (request.form.get("manual_delivery_address") or "").strip()
            if manual_address_text:
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.create_po"))

            delivery_address_id = request.form.get("delivery_address_id") or None
            if delivery_address_id == "manual":
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.create_po"))

            delivery_contact_id = request.form.get("delivery_contact_id") or None

            # ===== Contact rules =====
            if delivery_contact_id == "manual":
                if not delivery_address_id:
                    flash("Select a Delivery Address from the dropdown before adding a manual contact.", "danger")
                    return redirect(url_for("main.create_po"))

                manual_contact_name = (request.form.get("manual_contact_name") or "").strip()
                if not manual_contact_name:
                    flash("Manual contact name is required when manual contact is selected.", "danger")
                    return redirect(url_for("main.create_po"))

                metadata["manual_contact_name"]  = manual_contact_name
                metadata["manual_contact_phone"] = (request.form.get("manual_contact_phone") or "").strip()
                metadata["manual_contact_email"] = (request.form.get("manual_contact_email") or "").strip()
                metadata["delivery_contact_id"]  = None  # will be created in insert_po_bundle
            else:
                if delivery_contact_id:
                    contacts = fetch_delivery_contacts()
                    sel = next((c for c in contacts if c.get("id") == delivery_contact_id), None)
                    if not sel:
                        flash("Selected delivery contact not found.", "danger")
                        return redirect(url_for("main.create_po"))
                    delivery_address_id = sel.get("address_id") or delivery_address_id
                metadata["manual_contact_name"]  = None
                metadata["manual_contact_phone"] = None
                metadata["manual_contact_email"] = None
                metadata["delivery_contact_id"]  = delivery_contact_id or None

            metadata["delivery_address_id"]     = delivery_address_id
            metadata["manual_delivery_address"] = None

            po_id = insert_po_bundle(metadata)
            for item in line_items:
                item["po_id"] = po_id
            insert_line_items(line_items)

            flash("Purchase Order created successfully!", "success")
            return redirect(url_for("main.po_preview", po_id=po_id))
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Error creating PO: {e}", "danger")

    # -------- GET: render create form --------

    suppliers = suppliers_as_objects()
    suppliers_map = {s["id"]: s["name"] for s in suppliers}

    # Pull *directly* from project_register_items
    base, _ = _get_supabase_auth()
    hdr = get_headers(False)
    items = []
    try:
        r = requests.get(
            f"{base}/rest/v1/project_register_items",
            headers=hdr,
            params={
                "select": "projectnumber,item_seq,line_desc",
                "order": "projectnumber.desc,item_seq.asc",
                "limit": 100000,
            },
            timeout=30,
        )
        r.raise_for_status()
        items = r.json() or []
    except Exception as e:
        current_app.logger.warning("Failed to load project_register_items: %s", e)
        items = []

    # Build dropdown "<PN>-<SEQ> - <line_desc>"
    project_items = []
    for row in items:
        pn   = (row.get("projectnumber") or "").strip()
        if not pn:
            continue
        seq  = row.get("item_seq")
        seq_str = "" if seq is None else str(seq)
        desc = (row.get("line_desc") or "").strip()
        project_items.append({
            "projectnumber": pn,
            "item_seq": seq_str,
            "option_label": f"{pn}-{seq_str} - {desc}".rstrip(" -"),
        })

    delivery_addresses = fetch_delivery_addresses()
    delivery_contacts = fetch_delivery_contacts()

    idempotency_key = str(uuid.uuid4())
    session['last_form_token'] = idempotency_key

    return render_template(
        "po_form.html",
        mode="create",
        form_action=url_for("main.create_po"),
        po_data={},
        suppliers=suppliers,
        suppliers_map=suppliers_map,
        project_items=project_items,
        delivery_addresses=delivery_addresses,
        delivery_contacts=delivery_contacts,
        idempotency_key=idempotency_key,
    )

@main.route("/edit-po/<po_id>", methods=["GET", "POST"])
def edit_po(po_id):
    import uuid
    import requests
    from flask import session, render_template, request, redirect, url_for, flash, current_app

    def _to_int(val):
        try:
            return int(str(val).strip())
        except Exception:
            return None

    # ---- PATCH helpers (kept for the post-release optional-no-bump path) ----
    def _patch_po(po_id: str, fields: dict):
        base, _ = _get_supabase_auth()
        g = requests.get(
            f"{base}/rest/v1/purchase_orders?id=eq.{po_id}&select=*",
            headers=get_headers(False),
            timeout=20
        )
        g.raise_for_status()
        rows = g.json() or []
        if not rows:
            return {}

        allowed = set(rows[0].keys()) - {"id", "created", "modified", "active"}
        clean = {k: v for k, v in (fields or {}).items()
                 if k in allowed and k != "idempotency_key"}

        if "item_seq" in clean and clean["item_seq"] is not None:
            clean["item_seq"] = _to_int(clean["item_seq"])

        if not clean:
            return {}

        r = requests.patch(
            f"{base}/rest/v1/purchase_orders?id=eq.{po_id}&select=id",
            headers={**get_headers(), "Prefer": "return=representation"},
            json=clean,
            timeout=30
        )
        r.raise_for_status()
        return r.json()[0] if r.json() else {}

    def _patch_po_metadata(po_id: str, md_fields: dict):
        base, _ = _get_supabase_auth()
        g = requests.get(
            f"{base}/rest/v1/po_metadata?po_id=eq.{po_id}&active=is.true&select=*",
            headers=get_headers(False),
            timeout=20
        )
        g.raise_for_status()
        rows = g.json() or []
        if not rows:
            return {}

        allowed = set(rows[0].keys()) - {"id", "po_id", "created", "modified", "active"}
        clean = {k: v for k, v in (md_fields or {}).items()
                 if k in allowed and v is not None and k != "idempotency_key"}
        if not clean:
            return {}

        r = requests.patch(
            f"{base}/rest/v1/po_metadata?po_id=eq.{po_id}&active=is.true",
            headers={**get_headers(), "Prefer": "return=minimal"},
            json=clean,
            timeout=30
        )
        if r.status_code not in (200, 204):
            current_app.logger.error("PATCH po_metadata failed (%s): %s", r.status_code, r.text)
            r.raise_for_status()
        return {}

    def _replace_line_items(po_id: str, items: list):
        base, _ = _get_supabase_auth()
        rdel = requests.delete(
            f"{base}/rest/v1/po_line_items?po_id=eq.{po_id}",
            headers={**get_headers(), "Prefer": "return=minimal"},
            timeout=30
        )
        if rdel.status_code not in (200, 204):
            current_app.logger.error("DELETE po_line_items failed (%s): %s", rdel.status_code, rdel.text)
            rdel.raise_for_status()

        if items:
            payload = []
            for it in items:
                row = dict(it)
                for k in ("id", "created", "modified", "active"):
                    row.pop(k, None)
                row["po_id"] = po_id
                payload.append(row)

            rins = requests.post(
                f"{base}/rest/v1/po_line_items",
                headers={**get_headers(), "Prefer": "return=representation"},
                json=payload,
                timeout=30
            )
            rins.raise_for_status()
        return True

    def _is_numeric_ge_1(rev) -> bool:
        try:
            return int(str(rev).strip()) >= 1
        except Exception:
            return False

    def _coerce_rev_on_leaving_draft(prev_rev, old_status, new_status):
        # Keep your rule: when leaving draft, use numeric '1' if not already numeric
        if (old_status or '').lower() == 'draft' and (new_status or '').lower() != 'draft':
            return str(prev_rev).strip() if _is_numeric_ge_1(prev_rev) else '1'
        return prev_rev

    # ---------------- POST: handle save ----------------
    if request.method == "POST":
        try:
            # 🔒 Idempotency
            idem = request.form.get("idempotency_key")
            if not idem or idem != session.pop("last_form_token", None):
                flash("This form was already submitted or the token is invalid.", "warning")
                return redirect(url_for("main.edit_po", po_id=po_id))

            # 1) Load existing PO
            po = fetch_po_detail(po_id)
            if not po:
                flash("Original PO not found.", "danger")
                return redirect(url_for("main.index"))

            current_status = (po.get("status") or "draft").lower()
            current_rev    = po.get("current_revision", "a")
            if current_status in {"complete", "cancelled"}:
                flash("❌ This PO is marked as complete or cancelled and cannot be edited.", "warning")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # 2) Form & status
            new_status = (request.form.get("status") or po["status"]).lower()
            if current_status != "draft" and new_status == "draft":
                flash("❌ You cannot revert an approved PO back to draft.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))
            validate_po_status(new_status)

            metadata, line_items = parse_po_form(request.form)

            # Project/Item + flags
            selected_pn  = (request.form.get("project_id") or "").strip()
            selected_seq = _to_int(request.form.get("item_seq"))
            metadata["project_id"] = selected_pn or po.get("project_id")
            metadata["item_seq"]   = selected_seq if selected_seq is not None else _to_int(po.get("item_seq"))
            metadata["test_certificates_required"] = _f_bool("test_cert_required")

            # Delivery/address/contact (no manual address allowed)
            manual_address_text = (request.form.get("manual_delivery_address") or "").strip()
            if manual_address_text:
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            delivery_address_id = request.form.get("delivery_address_id") or None
            if delivery_address_id == "manual":
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            delivery_contact_raw = request.form.get("delivery_contact_id")
            delivery_contact_id  = delivery_contact_raw or None
            manual_contact_selected = (delivery_contact_raw == "manual")

            if manual_contact_selected:
                if not delivery_address_id:
                    flash("Select a Delivery Address from the dropdown before adding a manual contact.", "danger")
                    return redirect(url_for("main.edit_po", po_id=po_id))
                manual_contact_name = (request.form.get("manual_contact_name") or "").strip()
                if not manual_contact_name:
                    flash("Manual contact name is required when manual contact is selected.", "danger")
                    return redirect(url_for("main.edit_po", po_id=po_id))
                metadata["manual_contact_name"]  = manual_contact_name
                metadata["manual_contact_phone"] = (request.form.get("manual_contact_phone") or "").strip()
                metadata["manual_contact_email"] = (request.form.get("manual_contact_email") or "").strip()
                metadata["delivery_contact_id"]  = None
                metadata["idempotency_key"]      = idem
            else:
                if delivery_contact_id:
                    contacts = fetch_delivery_contacts()
                    sel = next((c for c in contacts if c.get("id") == delivery_contact_id), None)
                    if not sel:
                        flash("Selected delivery contact not found.", "danger")
                        return redirect(url_for("main.edit_po", po_id=po_id))
                    delivery_address_id = sel.get("address_id") or delivery_address_id
                metadata["manual_contact_name"]  = None
                metadata["manual_contact_phone"] = None
                metadata["manual_contact_email"] = None
                metadata["delivery_contact_id"]  = delivery_contact_id or None

            metadata["delivery_address_id"]     = delivery_address_id
            metadata["manual_delivery_address"] = None

            # 3) Bump rules
            # ALWAYS bump when current is draft (your new requirement)
            always_bump = (current_status == "draft")

            # after release, optional bump only for approved/issued
            bump_flag = (request.form.get("bump_revision") == "1")
            bump_allowed_after_release = (new_status in {"approved", "issued"})

            # --- Decide path ---
            if always_bump:
                # Bump from draft, lexicographic if staying in draft; else coerce to numeric on leaving draft
                next_rev = get_next_revision(str(current_rev).strip())
                target_rev = _coerce_rev_on_leaving_draft(next_rev, current_status, new_status)

                # Deactivate old snapshot
                deactivate_po_data(po_id)

                # New INSERT snapshot
                metadata["project_id"]              = metadata.get("project_id") or po.get("project_id")
                metadata["supplier_id"]             = po["supplier_id"]           # keep same supplier in new rev
                metadata["po_number"]               = po["po_number"]             # keep same number
                metadata["status"]                  = new_status
                metadata["current_revision"]        = target_rev
                metadata["delivery_address_id"]     = delivery_address_id
                metadata["manual_delivery_address"] = None
                metadata["item_seq"]                = metadata.get("item_seq") if metadata.get("item_seq") is not None else _to_int(po.get("item_seq"))

                new_po_id = insert_po_bundle(metadata)
                for item in line_items:
                    item["po_id"] = new_po_id
                insert_line_items(line_items)

                flash(f"PO revision created (rev {target_rev}).", "success")
                return redirect(url_for("main.po_preview", po_id=new_po_id))

            # Not in draft anymore:
            TERMINAL = {"issued", "complete", "cancelled"}

            # If user chose not to bump and it's allowed (approved/issued), do PATCH
            if bump_allowed_after_release and not bump_flag:
                po_fields = {
                    "project_id":        metadata.get("project_id") or po.get("project_id"),
                    "supplier_id":       po["supplier_id"],
                    "po_number":         po["po_number"],
                    "status":            new_status,
                    "current_revision":  current_rev,
                    "item_seq":          metadata.get("item_seq") if metadata.get("item_seq") is not None else _to_int(po.get("item_seq")),
                }
                md_fields = {k: v for k, v in metadata.items()
                             if k not in {"project_id","supplier_id","po_number","status","current_revision","item_seq","idempotency_key"}}

                _patch_po(po_id, po_fields)
                _patch_po_metadata(po_id, md_fields)
                _replace_line_items(po_id, line_items)

                flash("Changes saved (no revision bump).", "success")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # Otherwise: bump (new revision) for transitions/editing after release
            target_rev = get_next_revision(str(current_rev).strip()) \
                         if bump_flag else compute_updated_revision(current_rev, current_status, new_status)
            target_rev = _coerce_rev_on_leaving_draft(target_rev, current_status, new_status)

            deactivate_po_data(po_id)

            metadata["project_id"]              = metadata.get("project_id") or po.get("project_id")
            metadata["supplier_id"]             = po["supplier_id"]
            metadata["po_number"]               = po["po_number"]
            metadata["status"]                  = new_status
            metadata["current_revision"]        = target_rev
            metadata["delivery_address_id"]     = delivery_address_id
            metadata["manual_delivery_address"] = None
            metadata["item_seq"]                = metadata.get("item_seq") if metadata.get("item_seq") is not None else _to_int(po.get("item_seq"))

            new_po_id = insert_po_bundle(metadata)
            for item in line_items:
                item["po_id"] = new_po_id
            insert_line_items(line_items)

            flash(f"PO revision created successfully (rev {target_rev}).", "success")
            return redirect(url_for("main.po_preview", po_id=new_po_id))

        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Error creating revision: {e}", "danger")
            return redirect(url_for("main.edit_po", po_id=po_id))

    # ---------------- GET: render edit form ----------------
    po = fetch_po_detail(po_id)
    if not po:
        return render_template("404.html"), 404

    po_metadata = _active_po_metadata(po)
    suppliers = suppliers_as_objects()
    suppliers_map = {s["id"]: s["name"] for s in suppliers}

    delivery_contact = po.get("delivery_contact")
    delivery_address = po.get("delivery_address")

    delivery_address_id = None
    if isinstance(delivery_address, dict) and delivery_address.get("id"):
        delivery_address_id = delivery_address.get("id")
    elif isinstance(delivery_contact, dict) and delivery_contact.get("address_id"):
        delivery_address_id = delivery_contact.get("address_id")

    manual_contact_name  = (po_metadata.get("manual_contact_name")  or "")
    manual_contact_phone = (po_metadata.get("manual_contact_phone") or "")
    manual_contact_email = (po_metadata.get("manual_contact_email") or "")
    if not (manual_contact_name or manual_contact_phone or manual_contact_email):
        if isinstance(delivery_contact, dict):
            manual_contact_name  = (delivery_contact.get("name")  or manual_contact_name)
            manual_contact_phone = (delivery_contact.get("phone") or manual_contact_phone)
            manual_contact_email = (delivery_contact.get("email") or manual_contact_email)

    delivery_contact_id = delivery_contact.get("id") if isinstance(delivery_contact, dict) else None

    # --- Project / Item options (same as create) + fallback for current selection ---
    base, _ = _get_supabase_auth()
    hdr = get_headers(False)

    items = []
    try:
        r = requests.get(
            f"{base}/rest/v1/project_register_items",
            headers=hdr,
            params={
                "select": "projectnumber,item_seq,line_desc",
                "order": "projectnumber.desc,item_seq.asc",
                "limit": 100000,
            },
            timeout=30,
        )
        r.raise_for_status()
        items = r.json() or []
    except Exception as e:
        current_app.logger.warning("edit_po: failed to load project_register_items: %s", e)
        items = []

    project_items = []
    for row in items:
        pn   = (row.get("projectnumber") or "").strip()
        if not pn:
            continue
        seq  = row.get("item_seq")
        seq_str = "" if seq is None else str(seq)
        desc = (row.get("line_desc") or "").strip()
        project_items.append({
            "projectnumber": pn,
            "item_seq": seq_str,
            "option_label": f"{pn}-{seq_str} - {desc}".rstrip(" -"),
        })

    # Ensure current PN/SEQ appears even if not present in the register
    cur_pn  = (po.get("project_id") or "").strip()
    cur_seq = "" if po.get("item_seq") in (None, "") else str(po.get("item_seq")).strip()
    if cur_pn and cur_seq != "":
        found = any(
            (it.get("projectnumber","").strip() == cur_pn) and
            (str(it.get("item_seq","")).strip() == cur_seq)
            for it in project_items
        )
        if not found:
            project_items.append({
                "projectnumber": cur_pn,
                "item_seq": cur_seq,
                "option_label": f"{cur_pn}-{cur_seq} - (current selection)",
            })

    # Supplier id as string for template equality
    sup_id = po.get("supplier_id") or (po.get("supplier") or {}).get("id")
    supplier_id_str = str(sup_id) if sup_id else ""

    po_data = {
        "project_id": (po.get("project_id") or "").strip(),
        "item_seq": "" if po.get("item_seq") is None else str(po.get("item_seq")),
        "supplier_id": supplier_id_str,
        "delivery_terms": po_metadata.get("delivery_terms", ""),
        "delivery_date": po_metadata.get("delivery_date", ""),
        "shipping_method": po_metadata.get("shipping_method", ""),
        "test_cert_required": po_metadata.get("test_certificates_required", False),
        "po_number": po.get("po_number"),
        "revision": po.get("current_revision"),
        "status": po.get("status", "draft"),
        "manual_delivery_address": po.get("manual_delivery_address", ""),
        "line_items": po.get("line_items", []),
        "delivery_address_id": delivery_address_id,
        "delivery_contact_id": delivery_contact_id,
        "manual_contact_name": manual_contact_name,
        "manual_contact_phone": manual_contact_phone,
        "manual_contact_email": manual_contact_email,
        "supplier_reference_number": po_metadata.get("supplier_reference_number", ""),
    }

    idempotency_key = str(uuid.uuid4())
    session["last_form_token"] = idempotency_key

    _FLOW = ['draft', 'approved', 'issued', 'complete', 'cancelled']
    _cur  = (po.get("status") or 'draft').lower()
    try:
        _start = _FLOW.index(_cur)
    except ValueError:
        _start = 0
    statuses_forward = _FLOW[_start:]

    return render_template(
        "po_form.html",
        mode="edit",
        form_action=url_for("main.edit_po", po_id=po_id),
        po_data=po_data,
        statuses=statuses_forward,
        project_items=project_items,
        suppliers=suppliers,
        suppliers_map=suppliers_map,
        delivery_contacts=fetch_delivery_contacts(),
        delivery_addresses=fetch_delivery_addresses(),
        idempotency_key=idempotency_key,
    )


@main.route("/po/<po_id>/pdf")
def po_pdf(po_id):
    from .supabase_client import fetch_po_detail

    current_app.logger.info(f"📄 Route hit: PO PDF for {po_id}")

    try:
        po = fetch_po_detail(po_id)
        if not po:
            return render_template("404.html"), 404
    except Exception as e:
        flash(f"Failed to load PO: {e}", "danger")
        return redirect(url_for("main.po_list"))

    # Compute totals
    net_total = 0
    for item in po.get("line_items", []):
        qty = item.get("quantity") or 0
        price = item.get("unit_price") or 0.0
        item["total"] = qty * price
        net_total += item["total"]
    vat_total = net_total * 0.2
    grand_total = net_total + vat_total

    # Embed logo as base64
    logo_path = Path("app/static/img/PSS_Standard_RGB.png")
    with open(logo_path, "rb") as img_file:
        logo_base64 = base64.b64encode(img_file.read()).decode("utf-8")

    certs_table = load_certs_table()

    # Render HTML
    now = datetime.now()
    html = render_template(
        "po_pdf.html",
        po=po,
        net_total=net_total,
        vat_total=vat_total,
        grand_total=grand_total,
        now=now,
        logo_base64=logo_base64,
        pdf=True,
        certs_table=certs_table,
        include_certs_table=True
    )

    # Generate PDF (bytes in memory)
    pdf_bytes = HTML(string=html, base_url=request.root_url).write_pdf(
        stylesheets=[CSS(filename="app/static/css/pdf_style.css")]
    )

    # ==== NEW: Save an archive copy to network/share ====
    # Build filename: <ponumber>-<revision>.pdf
    po_number = str(po.get("po_number") or "UNKNOWN")
    revision = str(po.get("current_revision") or "NA")
    filename = f"{int(str(po_number)):06d}-{revision}.pdf"

    # Save directly into NETWORK_ARCHIVE_DIR (no subfolders now)

    archive_path = save_pdf_archive(pdf_bytes, relative_dir="", filename=filename)

    if archive_path:
        # Optional: pass a stable lock key so retries/page reloads won’t duplicate
        lock_key = f"{po.get('projectnumber') or po.get('project_number')}-{int(po.get('po_number') or 0):06d}-{filename}"

        try_create_po_draft(
            archive_path=archive_path,
            po=po,
            lock_key=lock_key,                 # ensures same key across both hits
            # mailbox_upn="purchasing@yourdomain.com",  # optional
            # to_recipients=["orders@supplier.com"],    # optional
            # cc_recipients=["buyer@yourco.com"],       # optional
            # lock_ttl_seconds=120,                     # optional, defaults to 120s
        )

    # Return PDF inline (unchanged behavior)
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename={filename}'
    return response


# app/email_po.py

email_bp = Blueprint("email_bp", __name__)

@email_bp.post("/po/<int:po_number>/email_draft")
def create_po_email_draft(po_number: int):
    """
    Creates a DRAFT email in Outlook with the PO PDF attached.
    Expected JSON body:
    {
      "project_number": "P-12345",
      "revision": "2",                       # optional; if you want in filename
      "pdf_path": "/app/output/006015-2.pdf",# absolute path inside container
      "to": ["supplier@example.com"],        # optional
      "cc": ["buyer@example.com"]            # optional
    }
    """
    data = request.get_json(force=True) or {}
    project_number = data.get("project_number")
    revision = data.get("revision")  # not required for subject, but handy for filename
    pdf_path = data.get("pdf_path")
    to_list = data.get("to") or []
    cc_list = data.get("cc") or []

    if not (project_number and pdf_path):
        return jsonify({"error": "project_number and pdf_path are required"}), 400

    # You can standardize the PO number (six digits) if needed:
    po_str = str(po_number).zfill(6)

    # Subject: "<Project Number> PO <PO Number>"
    subject = f"{project_number} PO {po_str}"

    # Body (exact text requested)
    body_text = (
        f"Please find attached PO {po_str} for previously quoted materials, "
        "please confirm as soon as possible and notify of any late or unavailable items.\n\n"
        "Best Regards,"
    )

    # Which mailbox should receive the draft?
    # Set MS_OUTLOOK_MAILBOX in your env (e.g., purchasing@yourdomain.com)
    mailbox_upn = current_app.config.get("MS_OUTLOOK_MAILBOX")
    if not mailbox_upn:
        # fallback to env for convenience
        import os
        mailbox_upn = os.environ.get("MS_OUTLOOK_MAILBOX")
    if not mailbox_upn:
        return jsonify({"error": "MS_OUTLOOK_MAILBOX not configured"}), 500

    try:
        draft = create_draft_with_attachment(
            mailbox_upn=mailbox_upn,
            subject=subject,
            body_text=body_text,
            pdf_path=pdf_path,
            to_recipients=to_list,
            cc_recipients=cc_list,
        )
        # Return a few useful fields; webLink opens the draft in Outlook on the web
        return jsonify({
            "status": "ok",
            "messageId": draft.get("id"),
            "webLink": draft.get("webLink"),
            "subject": draft.get("subject"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Bump revision route (insert a new PO row with next revision) ---

def _sb_base() -> str:
    return current_app.config["SUPABASE_URL"].rstrip("/")

def _sb_headers() -> dict:
    key = current_app.config["SUPABASE_API_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _get_po(po_id: str) -> dict:
    url = f"{_sb_base()}/rest/v1/purchase_orders?id=eq.{po_id}&select=*"
    r = requests.get(url, headers=_sb_headers(), timeout=20)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError("PO not found")
    return rows[0]

def _insert_po(row: dict) -> dict:
    url = f"{_sb_base()}/rest/v1/purchase_orders"
    r = requests.post(url, headers=_sb_headers(), json=row, timeout=30)
    r.raise_for_status()
    return r.json()[0]

def _clone_line_items(from_po_id: str, to_po_id: str) -> int:
    """Copy all line items from one PO to another. Adjust field names if needed."""
    base = _sb_base()
    hdr = _sb_headers()

    # pull existing items
    get_url = f"{base}/rest/v1/po_line_items?po_id=eq.{from_po_id}&select=*"
    gi = requests.get(get_url, headers=hdr, timeout=30)
    gi.raise_for_status()
    items = gi.json()

    if not items:
        return 0

    # strip identity fields; set new po_id
    payload = []
    for it in items:
        clean = {k: v for k, v in it.items() if k not in ("id", "created", "modified")}
        clean["po_id"] = to_po_id
        payload.append(clean)

    post_url = f"{base}/rest/v1/po_line_items"
    pi = requests.post(post_url, headers=hdr, json=payload, timeout=30)
    pi.raise_for_status()
    return len(payload)

@main.post("/po/<po_id>/advance-revision")
def advance_revision(po_id):
    try:
        cur = _get_po(po_id)

        # Only bump by choice; status remains the same (e.g., 'approved')
        next_rev = get_next_revision(str(cur.get("revision", "1")).strip())

        # Build new row by copying current, excluding identity/auto fields
        new_row = {k: v for k, v in cur.items()
                   if k not in ("id", "created", "modified")}
        new_row.update({
            "revision": next_rev,
            # If you prefer to force status:
            # "status": "approved",
        })

        created = _insert_po(new_row)

        # Optional: clone line items for the new revision
        try:
            _clone_line_items(cur["id"], created["id"])
        except Exception as e:
            current_app.logger.warning("Line item clone warning: %s", e)

        flash(f"Revision bumped to {next_rev}.", "success")
        return redirect(url_for("main.edit_po", po_id=created["id"]))

    except requests.HTTPError as e:
        current_app.logger.error("Advance revision failed: %s | %s", e, getattr(e.response, "text", ""))
        flash("Failed to bump revision.", "error")
        return redirect(url_for("main.edit_po", po_id=po_id))
    except Exception as e:
        current_app.logger.error("Advance revision error: %s", e)
        flash("Failed to bump revision.", "error")
        return redirect(url_for("main.edit_po", po_id=po_id))


@main.route("/spend-report")
def spend_report():


    # ---- Rolling 12 months (chronological; current month last) ----
    tz = ZoneInfo("Europe/London")
    now_local = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    months = []
    y, m = now_local.year, now_local.month
    for i in range(11, -1, -1):
        yy, mm = y, m - (11 - i)
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}-01")
    months = sorted(months)  # ensures current month is last
    first_month_start = months[0]
    next_month_year, next_month = (y + 1, 1) if m == 12 else (y, m + 1)
    next_month_start = f"{next_month_year:04d}-{next_month:02d}-01"

    # ---- Build month boundaries for linking (from / to) ----
    def _next_month_key(mkey: str) -> str:
        # mkey is "YYYY-MM-01"
        y = int(mkey[0:4])
        m = int(mkey[5:7])
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        return f"{ny:04d}-{nm:02d}-01"

    month_from = {m: m for m in months}
    month_to = {m: _next_month_key(m) for m in months}

    # ---- Latest-only totals from accounts_overview ----
    ao_rows = fetch_accounts_overview_latest(statuses=("approved", "issued", "complete"))
    by_po_number = {str(r["po_number"]): r for r in ao_rows if r.get("po_number") is not None}
    po_numbers = list(by_po_number.keys())

    # Get latest issued per PO (no date filter); THEN apply the 12-month window here
    last_issued = fetch_last_issued_dates_any(po_numbers)

    # Map pn -> month key
    pn_to_month = {}
    for pn, issued_dt in last_issued.items():
        if not issued_dt:
            continue
        mkey = str(issued_dt)[:7] + "-01"
        # only keep if inside our rolling 12 months
        if mkey in months:
            pn_to_month[pn] = mkey

    # Build pivot using totals from latest row in accounts_overview
    data = {}
    for pn, mkey in pn_to_month.items():
        ao = by_po_number.get(pn)
        if not ao:
            continue
        project = ao.get("projectnumber") or "—"
        total_val = float(ao.get("total_value") or 0.0)
        data.setdefault(project, {}).setdefault(mkey, 0.0)
        data[project][mkey] += total_val

    # ---- Totals ----
    row_totals = {}
    col_totals = {m: 0.0 for m in months}
    grand_total = 0.0
    for proj, spends in data.items():
        total = sum(spends.get(m, 0.0) for m in months)
        row_totals[proj] = total
        grand_total += total
        for m in months:
            col_totals[m] += spends.get(m, 0.0)

    # ---- Natural sort by project number ----
    def _natural_key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]
    project_order = sorted(data.keys(), key=_natural_key)

    return render_template(
        "spend_report.html",
        months=months,
        data=data,
        project_order=project_order,
        row_totals=row_totals,
        col_totals=col_totals,
        grand_total=grand_total,
        month_from=month_from,   # <-- added
        month_to=month_to        # <-- added
    )

