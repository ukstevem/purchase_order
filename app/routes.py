from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response, jsonify, current_app
from app.supabase_client import fetch_suppliers, fetch_delivery_addresses, fetch_projects, insert_po_bundle, insert_line_items, fetch_delivery_contacts
from app.utils.forms import parse_po_form
from app.utils.project_filter import get_project_id_by_number
from app.supabase_client import fetch_all_pos, fetch_active_pos, fetch_project_po_summary
from app.utils.status_utils import (
    allowed_next_statuses, 
    is_forward_or_same, 
    coerce_rev_on_leaving_draft
    )
from app.utils.revision import compute_updated_revision
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
from app.utils.revision import get_next_revision

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

@main.route("/po-list")
def po_list():
    projectnumber = request.args.get("projectnumber")
    project_id = None
    try:
        if projectnumber:
            project_id = get_project_id_by_number(projectnumber)

        pos = fetch_active_pos(project_id=project_id)
    except Exception as e:
        flash(f"Failed to load POs: {e}", "danger")
        pos = []

    return render_template("po_list.html", pos=pos, projectnumber=projectnumber)

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
            # 🔒 Idempotency: verify one-shot token
            idem = request.form.get("idempotency_key")
            if not idem or idem != session.pop("last_form_token", None):
                flash("This form was already submitted or the token is invalid.", "warning")
                return redirect(url_for("main.create_po"))
            
            metadata, line_items = parse_po_form(request.form)
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
                # user somehow picked the manual option in UI
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.create_po"))

            delivery_contact_id = request.form.get("delivery_contact_id") or None

            # ===== Contact rules =====
            if delivery_contact_id == "manual":
                # Must have a dropdown-selected address to satisfy FK on delivery_contacts.address_id
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
                # Existing contact selected (UUID) or none
                if delivery_contact_id:
                    # Align address to contact’s address_id to keep data consistent
                    contacts = fetch_delivery_contacts()
                    sel = next((c for c in contacts if c.get("id") == delivery_contact_id), None)
                    if not sel:
                        flash("Selected delivery contact not found.", "danger")
                        return redirect(url_for("main.create_po"))
                    # Force address to match the contact
                    delivery_address_id = sel.get("address_id") or delivery_address_id
                # Clear manual fields
                metadata["manual_contact_name"]  = None
                metadata["manual_contact_phone"] = None
                metadata["manual_contact_email"] = None
                metadata["delivery_contact_id"]  = delivery_contact_id or None

            # Persist only dropdown address (or None). No manual text!
            metadata["delivery_address_id"] = delivery_address_id
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

    suppliers = fetch_suppliers()
    projects = fetch_projects()
    delivery_addresses = fetch_delivery_addresses()
    delivery_contacts = fetch_delivery_contacts()

    projects_sorted = sorted(projects, key=lambda p: int(p['projectnumber']), reverse=True)

    idempotency_key = str(uuid.uuid4())
    session['last_form_token'] = idempotency_key

    return render_template(
        "po_form.html",
        mode="create",
        form_action=url_for("main.create_po"),
        po_data={},
        suppliers=suppliers,
        projects=projects_sorted,
        delivery_addresses=delivery_addresses,
        delivery_contacts=delivery_contacts,
        idempotency_key=idempotency_key,
    )

@main.route("/edit-po/<po_id>", methods=["GET", "POST"])
def edit_po(po_id):
    import uuid
    import requests
    from flask import current_app, session, render_template, request, redirect, url_for, flash

    # --- tiny Supabase helpers (local to this route) ---
    def _sb():
        base = current_app.config["SUPABASE_URL"].rstrip("/")
        key  = current_app.config["SUPABASE_API_KEY"]
        hdr  = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        return base, hdr

    def _patch_po(po_id: str, fields: dict):
        base, hdr = _sb()
        clean = {k: v for k, v in (fields or {}).items() if k != "idempotency_key"}
        if not clean:
            return {}
        r = requests.patch(f"{base}/rest/v1/purchase_orders?id=eq.{po_id}",
                           headers=hdr, json=clean, timeout=30)
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            current_app.logger.error("PATCH purchase_orders failed: %s | %s", e, r.text)
            raise
        return r.json()[0] if r.json() else {}

    def _patch_po_metadata(po_id: str, md_fields: dict):
        """
        Patch only columns that exist on the *active* po_metadata row for this po_id.
        Also drop None values to avoid NOT NULL violations.
        """
        base, hdr = _sb()

        # Discover existing columns on the active metadata row
        g = requests.get(
            f"{base}/rest/v1/po_metadata?po_id=eq.{po_id}&active=is.true&select=*",
            headers=hdr, timeout=20
        )
        try:
            g.raise_for_status()
        except requests.HTTPError as e:
            current_app.logger.error("GET po_metadata failed: %s | %s", e, g.text)
            raise

        rows = g.json()
        if not rows:
            # Nothing to patch; silently succeed
            return {}

        allowed = set(rows[0].keys()) - {"id", "po_id", "created", "modified", "active"}
        clean = {
            k: v for k, v in (md_fields or {}).items()
            if k in allowed and v is not None and k != "idempotency_key"
        }
        if not clean:
            return {}

        # For PATCHing metadata we don't need a representation; avoid any edge cases
        hdr_min = dict(hdr)
        hdr_min["Prefer"] = "return=minimal"

        r = requests.patch(
            f"{base}/rest/v1/po_metadata?po_id=eq.{po_id}&active=is.true",
            headers=hdr_min, json=clean, timeout=30
        )
        if r.status_code not in (200, 204):
            current_app.logger.error("PATCH po_metadata failed (%s): %s", r.status_code, r.text)
            r.raise_for_status()
        return {}

    def _replace_line_items(po_id: str, items: list):
        base, hdr = _sb()
        # delete existing items for this po_id
        rdel = requests.delete(f"{base}/rest/v1/po_line_items?po_id=eq.{po_id}",
                               headers=hdr, timeout=30)
        if rdel.status_code not in (200, 204):
            current_app.logger.error("DELETE po_line_items failed (%s): %s", rdel.status_code, rdel.text)
            rdel.raise_for_status()
        # insert new
        if items:
            payload = []
            for it in items:
                row = dict(it)
                # scrub identity/flags that shouldn't be client-set
                for k in ("id", "created", "modified", "active"):
                    row.pop(k, None)
                row["po_id"] = po_id
                payload.append(row)
            rins = requests.post(f"{base}/rest/v1/po_line_items",
                                 headers=hdr, json=payload, timeout=30)
            try:
                rins.raise_for_status()
            except requests.HTTPError as e:
                current_app.logger.error("POST po_line_items failed: %s | %s", e, rins.text)
                raise
        return True

    # --- revision helpers (local to this route) ---
    def _is_numeric_ge_1(rev) -> bool:
        try:
            return int(str(rev).strip()) >= 1
        except Exception:
            return False

    def _coerce_rev_on_leaving_draft(prev_rev, old_status, new_status):
        """
        If moving out of 'draft', set revision to '1' unless it's already numeric >= 1.
        Fires on any move from draft up the chain.
        """
        if (old_status or '').lower() == 'draft' and (new_status or '').lower() != 'draft':
            return str(prev_rev).strip() if _is_numeric_ge_1(prev_rev) else '1'
        return prev_rev

    from .supabase_client import (
        fetch_po_detail,
        deactivate_po_data,
        insert_po_bundle,
        insert_line_items,
        fetch_projects,
        fetch_suppliers,
        fetch_delivery_addresses,
        fetch_delivery_contacts,
    )
    from .utils.revision import get_next_revision, compute_updated_revision
    from .utils.status_utils import validate_po_status, POStatus

    # ---------------- POST: create OR patch ----------------
    if request.method == "POST":
        try:
            # 🔒 Idempotency: one-shot token
            idem = request.form.get("idempotency_key")
            if not idem or idem != session.pop("last_form_token", None):
                flash("This form was already submitted or the token is invalid.", "warning")
                return redirect(url_for("main.edit_po", po_id=po_id))

            # 1) Fetch the existing PO with expansions
            po = fetch_po_detail(po_id)
            if not po:
                flash("Original PO not found.", "danger")
                return redirect(url_for("main.index"))

            current_status = (po.get("status") or "draft").lower()
            current_rev    = po.get("current_revision", "a")
            if current_status in {"complete", "cancelled"}:
                flash("❌ This PO is marked as complete or cancelled and cannot be edited.", "warning")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # 2) Parse form & validate status transition
            new_status = (request.form.get("status") or po["status"]).lower()
            if current_status != "draft" and new_status == "draft":
                flash("❌ You cannot revert an approved PO back to draft.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))
            validate_po_status(new_status)

            # Parse form into metadata + line_items (your existing helper)
            metadata, line_items = parse_po_form(request.form)
            metadata["test_certificates_required"] = _f_bool("test_cert_required")

            # Disallow manual address text
            manual_address_text = (request.form.get("manual_delivery_address") or "").strip()
            if manual_address_text:
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            delivery_address_id = request.form.get("delivery_address_id") or None
            if delivery_address_id == "manual":
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            # Delivery contact selection
            delivery_contact_raw = request.form.get("delivery_contact_id")
            delivery_contact_id  = delivery_contact_raw or None
            manual_contact_selected = (delivery_contact_raw == "manual")

            if manual_contact_selected:
                # Must have an address selected (FK)
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
                # Existing contact selected or none
                if delivery_contact_id:
                    contacts = fetch_delivery_contacts()
                    sel = next((c for c in contacts if c.get("id") == delivery_contact_id), None)
                    if not sel:
                        flash("Selected delivery contact not found.", "danger")
                        return redirect(url_for("main.edit_po", po_id=po_id))
                    # Align address with the contact’s address
                    delivery_address_id = sel.get("address_id") or delivery_address_id
                metadata["manual_contact_name"]  = None
                metadata["manual_contact_phone"] = None
                metadata["manual_contact_email"] = None
                metadata["delivery_contact_id"]  = delivery_contact_id or None

            # Persist only dropdown address (or None). No manual text!
            metadata["delivery_address_id"]     = delivery_address_id
            metadata["manual_delivery_address"] = None

            # 🔁 Bump flag & allowance (only approved/issued are allowed to bump)
            bump = (request.form.get("bump_revision") == "1")
            bump_allowed = new_status in {"approved", "issued"}
            if manual_contact_selected:
                # Force INSERT path if creating a manual contact downstream
                bump = True

            # ---- PATH A: terminal flips (issued/complete/cancelled)
            TERMINAL = {"issued", "complete", "cancelled"}
            if new_status in TERMINAL:

                # If user asked to bump *and* it's allowed (approved/issued), do a new revision snapshot
                if bump and bump_allowed:
                    target_rev = get_next_revision(str(current_rev).strip())
                    target_rev = _coerce_rev_on_leaving_draft(target_rev, current_status, new_status)

                    # Deactivate old metadata/items for this PO row
                    deactivate_po_data(po_id)

                    # Build new metadata row for INSERT (clone + overlay)
                    metadata["project_id"]              = po["project_id"]
                    metadata["supplier_id"]             = po["supplier_id"]
                    metadata["po_number"]               = po["po_number"]
                    metadata["status"]                  = new_status
                    metadata["current_revision"]        = target_rev
                    metadata["delivery_address_id"]     = delivery_address_id
                    metadata["manual_delivery_address"] = None

                    # Create a brand new PO row for the new revision + insert fresh items snapshot
                    new_po_id = insert_po_bundle(metadata)
                    for item in line_items:
                        item["po_id"] = new_po_id
                    insert_line_items(line_items)

                    flash(f"PO revision created successfully (rev {target_rev}).", "success")
                    return redirect(url_for("main.po_preview", po_id=new_po_id))

                # Otherwise: NO bump → patch in place and still write the items
                PO_KEYS = {"project_id", "supplier_id", "po_number", "status", "current_revision"}
                coerced_rev = _coerce_rev_on_leaving_draft(current_rev, current_status, new_status)

                po_fields = {
                    "project_id":       po["project_id"],
                    "supplier_id":      po["supplier_id"],
                    "po_number":        po["po_number"],
                    "status":           new_status,
                    "current_revision": coerced_rev,
                }
                md_fields = {k: v for k, v in metadata.items() if k not in PO_KEYS and k != "idempotency_key"}

                _patch_po(po_id, po_fields)
                _patch_po_metadata(po_id, md_fields)

                # 🔴 Ensure items are saved on terminal transitions without bump
                _replace_line_items(po_id, line_items)

                flash(f"Status set to {new_status} (rev {po_fields['current_revision']}).", "success")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # ---- PATH B: Approved + NO bump -> PATCH in place (keep same revision, replace items)
            if current_status == "approved" and new_status == "approved" and not bump:
                PO_KEYS = {"project_id", "supplier_id", "po_number", "status", "current_revision"}
                po_fields = {
                    "project_id":       po["project_id"],
                    "supplier_id":      po["supplier_id"],
                    "po_number":        po["po_number"],
                    "status":           "approved",
                    "current_revision": current_rev,  # unchanged
                }
                md_fields = {k: v for k, v in metadata.items() if k not in PO_KEYS and k != "idempotency_key"}

                _patch_po(po_id, po_fields)
                _patch_po_metadata(po_id, md_fields)
                _replace_line_items(po_id, line_items)

                flash("Changes saved (no revision bump).", "success")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # ---- PATH C: INSERT new row as a new revision (deactivate old first)
            # 4) Decide the target revision
            if bump:
                # Explicit bump (e.g., a -> b or 1 -> 2)
                target_rev = get_next_revision(str(current_rev).strip())
            else:
                # Your rule: originally only Draft -> Approved = "1"; we extend to any move out of draft
                target_rev = compute_updated_revision(current_rev, current_status, new_status)
                # If still in DRAFT and revision didn't change, auto-advance alpha to avoid duplicate key
                if current_status == "draft" and new_status == "draft" and str(target_rev) == str(current_rev):
                    target_rev = get_next_revision(str(current_rev).strip())

            # ensure: leaving draft (to approved/issued/complete/cancelled) -> '1' unless already >=1
            target_rev = _coerce_rev_on_leaving_draft(target_rev, current_status, new_status)

            # 3) Deactivate current revision data rows (now that we know we will insert)
            deactivate_po_data(po_id)

            # 5) Prepare updated metadata for INSERT
            metadata["project_id"]              = po["project_id"]
            metadata["supplier_id"]             = po["supplier_id"]
            metadata["po_number"]               = po["po_number"]
            metadata["status"]                  = new_status
            metadata["current_revision"]        = target_rev
            metadata["delivery_address_id"]     = delivery_address_id
            metadata["manual_delivery_address"] = None

            # 6) Insert new PO + items
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

    # ---------------- GET: render edit form (unchanged except statuses list) ----------------
    from .supabase_client import fetch_po_detail
    po = fetch_po_detail(po_id)
    if not po:
        return render_template("404.html"), 404

    # ✅ Safely get active metadata (handles None, list, or dict)
    po_metadata = _active_po_metadata(po)  # existing helper

    # Linked objects (may be None if not expanded by your fetch)
    delivery_contact = po.get("delivery_contact")
    delivery_address = po.get("delivery_address")

    # Resolve delivery_address_id
    delivery_address_id = None
    if isinstance(delivery_address, dict) and delivery_address.get("id"):
        delivery_address_id = delivery_address.get("id")
    elif isinstance(delivery_contact, dict) and delivery_contact.get("address_id"):
        delivery_address_id = delivery_contact.get("address_id")

    # Manual contact values from metadata (safe defaults)
    manual_contact_name  = (po_metadata.get("manual_contact_name")  or "")
    manual_contact_phone = (po_metadata.get("manual_contact_phone") or "")
    manual_contact_email = (po_metadata.get("manual_contact_email") or "")

    # If metadata empty but a delivery_contact exists, optionally prefill from it
    if not (manual_contact_name or manual_contact_phone or manual_contact_email):
        if isinstance(delivery_contact, dict):
            manual_contact_name  = (delivery_contact.get("name")  or manual_contact_name)
            manual_contact_phone = (delivery_contact.get("phone") or manual_contact_phone)
            manual_contact_email = (delivery_contact.get("email") or manual_contact_email)

    # Resolve delivery_contact_id (if relation expanded)
    delivery_contact_id = delivery_contact.get("id") if isinstance(delivery_contact, dict) else None

    # Build po_data for template
    po_data = {
        "project_id": po["project_id"],
        "supplier_id": po["supplier_id"],
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

    # Generate a one-shot token for the edit form
    idempotency_key = str(uuid.uuid4())
    session["last_form_token"] = idempotency_key

    # Build forward-only statuses for the dropdown
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
        statuses=statuses_forward,  # forward-only list
        projects=fetch_projects(),
        suppliers=fetch_suppliers(),
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
    filename = f"{int(po_number):06d}-{revision}.pdf"

    # Save directly into NETWORK_ARCHIVE_DIR (no subfolders now)
    archive_path = save_pdf_archive(pdf_bytes, relative_dir="", filename=filename)

    # Only attempt if archiving worked
    if archive_path:
        try_create_po_draft(
            archive_path=archive_path,
            po=po,                                               # pass the PO dict you already have
            mailbox_upn="purchasing@powersystemservices.co.uk",  # optional; else uses env MS_OUTLOOK_MAILBOX
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
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from flask import render_template
    from app.supabase_client import (
        fetch_accounts_overview_latest,
        fetch_po_updated_at_for_ids_in_window,
    )
    import re

    def _natural_key(s: str):
        # splits “P123-01” into ["P", 123, "-", 1] so numbers sort numerically
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


    # ---- Rolling 12 months (Europe/London), oldest -> newest (current month far right) ----
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
    first_month_start = months[0]
    next_month_year, next_month = (y + 1, 1) if m == 12 else (y, m + 1)
    next_month_start = f"{next_month_year:04d}-{next_month:02d}-01"

    months = sorted(months)  # "YYYY-MM-01" sorts lexicographically in date order

    # ---- 1) Latest-only rows with totals from accounts_overview ----
    ao_rows = fetch_accounts_overview_latest(statuses=("approved", "issued", "complete"))
    ao_by_id = {r["id"]: r for r in ao_rows}
    latest_ids = list(ao_by_id.keys())

    # ---- 2) For those latest IDs, get updated_at in our window ----
    po_rows = fetch_po_updated_at_for_ids_in_window(latest_ids, first_month_start, next_month_start)
    # Map id -> month key ("YYYY-MM-01") for rows inside window
    id_to_month = {}
    for r in po_rows:
        updated_at = r.get("updated_at")
        if not updated_at:
            continue
        mkey = str(updated_at)[:7] + "-01"
        if mkey in months:
            id_to_month[r["id"]] = mkey

    # ---- 3) Pivot: rows = project (projectnumber), cols = months, val = total_value ----
    data = {}
    for po_id, mkey in id_to_month.items():
        ao = ao_by_id.get(po_id)
        if not ao:
            continue
        project = ao.get("projectnumber") or "—"
        total_val = float(ao.get("total_value") or 0.0)
        if total_val == 0.0:
            # keep or drop zeros; choose your preference. Drop to keep the table tidy:
            continue
        data.setdefault(project, {}).setdefault(mkey, 0.0)
        data[project][mkey] += total_val

    project_order = sorted(data.keys(), key=_natural_key)

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

    return render_template(
        "spend_report.html",
        months=months,
        data=data,
        project_order=project_order,
        row_totals=row_totals,
        col_totals=col_totals,
        grand_total=grand_total,
    )
