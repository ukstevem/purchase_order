from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response
from app.supabase_client import fetch_suppliers, fetch_delivery_addresses, fetch_projects, insert_po_bundle, insert_line_items, fetch_delivery_contacts
from app.utils.forms import parse_po_form
from app.utils.project_filter import get_project_id_by_number
from app.supabase_client import fetch_all_pos, fetch_active_pos, fetch_project_po_summary
from app.utils.status_utils import POStatus, validate_po_status
from app.utils.revision import compute_updated_revision
from app.utils.pdf_archive import save_pdf_archive
from weasyprint import HTML, CSS
from datetime import datetime, date
from flask import current_app, render_template, request, session, flash
from .utils.certs_table import load_certs_table
from werkzeug.utils import secure_filename
import base64, uuid
from pathlib import Path

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

    # print("📦 projectnumber from request.args:", projectnumber)
    return render_template("po_list.html", pos=pos, projectnumber=projectnumber)

@main.route("/po/<po_id>")
def po_preview(po_id):
    from .supabase_client import fetch_po_detail
    # print(f"📄 Route hit: PO preview for {po_id}")
    try:
        po = fetch_po_detail(po_id)
        print(f"PO Preview Data : {po}")
        if not po:
            print("⚠️ PO not found or empty")
            return render_template("404.html"), 404
        # print(f"✅ Fetched PO: {po.get('po_number', 'N/A')}")
    except Exception as e:
        print(f"❌ Exception fetching PO: {e}")
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

    return render_template("po_web.html", po=po, grand_total=grand_total, vat_total=vat_total, net_total=net_total, now=datetime.now())

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
    from .supabase_client import (
        fetch_po_detail,
        deactivate_po_data,
        insert_po_bundle,
        insert_line_items,
        fetch_projects,
        fetch_suppliers,
        fetch_delivery_addresses,   # ✅ needed in GET render
        fetch_delivery_contacts,    # ✅ needed in GET render
    )
    from .utils.revision import get_next_revision, compute_updated_revision  # ✅ import compute_updated_revision
    from .utils.status_utils import validate_po_status, POStatus

    # ---------------- POST: create a new revision ----------------
    if request.method == "POST":
        try:
            # 🔒 Idempotency: verify one-shot token
            idem = request.form.get("idempotency_key")
            if not idem or idem != session.pop("last_form_token", None):
                flash("This form was already submitted or the token is invalid.", "warning")
                return redirect(url_for("main.edit_po", po_id=po_id))
            
            # 1) Fetch the existing PO
            po = fetch_po_detail(po_id)
            if not po:
                flash("Original PO not found.", "danger")
                return redirect(url_for("main.index"))

            if po["status"].lower() in {"complete", "cancelled"}:
                flash("❌ This PO is marked as complete or cancelled and cannot be edited.", "warning")
                return redirect(url_for("main.po_preview", po_id=po_id))

            # 2) Parse form, validate status transition, and resolve delivery address/contact
            new_status = request.form.get("status", "draft").lower()
            if po["status"] != "draft" and new_status == "draft":
                flash("❌ You cannot revert an approved PO back to draft.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))
            validate_po_status(new_status)

            current_status = po.get("status", "draft").lower()
            current_rev    = po.get("current_revision", "a")

            # Parse form now (we'll also resolve delivery address / contact here)
            metadata, line_items = parse_po_form(request.form)
            metadata["test_certificates_required"] = _f_bool("test_cert_required")

            # 🚫 No manual delivery address allowed
            manual_address_text = (request.form.get("manual_delivery_address") or "").strip()
            if manual_address_text:
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            delivery_address_id = request.form.get("delivery_address_id") or None
            if delivery_address_id == "manual":
                flash("Manual delivery address is not allowed. Please select a Delivery Address from the dropdown.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            # Handle delivery contact selection
            delivery_contact_id = request.form.get("delivery_contact_id") or None
            if delivery_contact_id == "manual":
                # Must have a dropdown-selected address to satisfy FK on delivery_contacts.address_id
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
                metadata["delivery_contact_id"]  = None  # will be created in insert_po_bundle if needed
                metadata["idempotency_key"] = idem
            else:
                # Existing contact selected (UUID) or none
                if delivery_contact_id:
                    # Align address to the selected contact’s address_id to keep data consistent
                    contacts = fetch_delivery_contacts()
                    sel = next((c for c in contacts if c.get("id") == delivery_contact_id), None)
                    if not sel:
                        flash("Selected delivery contact not found.", "danger")
                        return redirect(url_for("main.edit_po", po_id=po_id))
                    # Force address to match the contact
                    delivery_address_id = sel.get("address_id") or delivery_address_id

                # Clear manual fields
                metadata["manual_contact_name"]  = None
                metadata["manual_contact_phone"] = None
                metadata["manual_contact_email"] = None
                metadata["delivery_contact_id"]  = delivery_contact_id or None

            # Persist only dropdown address (or None). No manual text!
            metadata["delivery_address_id"]   = delivery_address_id
            metadata["manual_delivery_address"] = None


            # 3) Deactivate current revision data rows
            deactivate_po_data(po_id)

            # 4. Prepare updated metadata
            metadata["project_id"]          = po["project_id"]
            metadata["supplier_id"]         = po["supplier_id"]
            metadata["po_number"]           = po["po_number"]
            metadata["status"]              = new_status
            metadata["current_revision"]    = compute_updated_revision(current_rev, current_status, new_status)
            metadata["delivery_address_id"] = delivery_address_id
            metadata["manual_delivery_address"] = None

            # 5) Insert new PO + items
            new_po_id = insert_po_bundle(metadata)
            for item in line_items:
                item["po_id"] = new_po_id
            insert_line_items(line_items)

            flash("PO revision created successfully.", "success")
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

    # ✅ Safely get active metadata (handles None, list, or dict)
    po_metadata = _active_po_metadata(po)

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
    }

    # Generate a one-shot token for the edit form
    idempotency_key = str(uuid.uuid4())
    session['last_form_token'] = idempotency_key

    return render_template(
        "po_form.html",
        mode="edit",
        form_action=url_for("main.edit_po", po_id=po_id),
        po_data=po_data,
        statuses=[s.value for s in POStatus],
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
    revision = str(po.get("revision") or "a")
    filename = f"{po_number:06d}-{revision}.pdf"

    # Save directly into NETWORK_ARCHIVE_DIR (no subfolders now)
    save_pdf_archive(pdf_bytes, relative_dir="", filename=filename)
    # ================================================

    # Return PDF inline (unchanged behavior)
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'inline; filename={filename}'
    return response
