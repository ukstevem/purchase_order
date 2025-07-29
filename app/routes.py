from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response
from app.supabase_client import fetch_suppliers, fetch_delivery_addresses, fetch_projects, insert_po_bundle, insert_line_items, fetch_delivery_contacts
from app.utils.forms import parse_po_form
from app.utils.project_filter import get_project_id_by_number
from app.supabase_client import fetch_all_pos, fetch_active_pos
from app.utils.status_utils import POStatus, validate_po_status
from app.utils.revision import compute_updated_revision
from weasyprint import HTML, CSS
from datetime import datetime
from flask import current_app
import requests
import base64
from pathlib import Path

main = Blueprint("main", __name__)

@main.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404

@main.route("/")
def index():
    return {"message": "PO System running"}

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
            metadata, line_items = parse_po_form(request.form)
            metadata["status"] = "draft"
            metadata["current_revision"] = "a"

            address_id = request.form.get("delivery_address_id")
            manual_address = request.form.get("manual_delivery_address", "").strip()

            if address_id == "manual":
                metadata["delivery_address_id"] = None
                metadata["manual_delivery_address"] = manual_address
            else:
                metadata["delivery_address_id"] = address_id
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

    return render_template(
        "po_form.html",
        mode="create",
        form_action=url_for("main.create_po"),
        po_data={},
        suppliers=suppliers,
        projects=projects_sorted,
        delivery_addresses=delivery_addresses,
        delivery_contacts =delivery_contacts
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
    )
    from .utils.revision import get_next_revision
    from .utils.status_utils import validate_po_status, POStatus

    if request.method == "POST":
        try:
            # 1. Fetch the existing PO
            po = fetch_po_detail(po_id)
            if not po:
                flash("Original PO not found.", "danger")
                return redirect(url_for("main.index"))

            if po["status"].lower() in {"complete", "cancelled"}:
                flash("❌ This PO is marked as complete or cancelled and cannot be edited.", "warning")
                return redirect(url_for("main.po_preview", po_id=po_id))


            # 2. Parse form and validate status
            new_status = request.form.get("status", "draft").lower()
            if po["status"] != "draft" and new_status == "draft":
                flash("❌ You cannot revert an approved PO back to draft.", "danger")
                return redirect(url_for("main.edit_po", po_id=po_id))

            validate_po_status(new_status)

            current_status = po.get("status", "draft").lower()
            current_rev = po.get("current_revision", "a")

            # 3. Deactivate current revision
            deactivate_po_data(po_id)

            # 4. Parse metadata and line items
            metadata, line_items = parse_po_form(request.form)

            # 5. Prepare updated metadata
            metadata["project_id"] = po["project_id"]
            metadata["supplier_id"] = po["supplier_id"]
            metadata["po_number"] = po["po_number"]
            metadata["status"] = new_status
            metadata["current_revision"] = compute_updated_revision(
                current_rev,
                current_status,
                new_status,
            )
            metadata["delivery_address_id"] = request.form.get("delivery_address_id") or None
            metadata["manual_delivery_address"] = request.form.get("manual_delivery_address", "").strip() or None
            metadata["delivery_contact_id"] = request.form.get("delivery_contact_id") or None

            # 6. Insert new PO + items
            # print("DEBUG METADATA:", metadata)
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

    # GET request
    po = fetch_po_detail(po_id)
    delivery_contact = po.get("delivery_contact")
    delivery_address = po.get("delivery_address")

    print(delivery_address, delivery_contact)

    # Fallback: use address from contact if no direct delivery address
    if not delivery_address and delivery_contact:
        # This will be fetched during fetch_po_detail, so should exist
        address_id = delivery_contact.get("address_id")
        if address_id:
            delivery_address = {"id": address_id}

    if not po:
        return render_template("404.html"), 404

    po_data = {
        "project_id": po["project_id"],
        "supplier_id": po["supplier_id"],
        "delivery_terms": po.get("po_metadata", {}).get("delivery_terms", ""),
        "delivery_date": po.get("po_metadata", {}).get("delivery_date", ""),
        "shipping_method": po.get("po_metadata", {}).get("shipping_method", ""),
        "test_cert_required": po.get("po_metadata", {}).get("test_certificates_required", False),
        "po_number": po.get("po_number"),
        "revision": po.get("current_revision"),
        "status": po.get("status", "draft"),
        "manual_delivery_address": po.get("manual_delivery_address", ""),
        "line_items": po.get("line_items", []),
        "delivery_address_id": delivery_address.get("id") if delivery_address else None,
        "delivery_contact_id": delivery_contact.get("id") if delivery_contact else None,
    }

    return render_template(
        "po_form.html",
        mode="edit",
        form_action=url_for("main.edit_po", po_id=po_id),
        po_data=po_data,
        statuses=[s.value for s in POStatus],
        projects=fetch_projects(),
        suppliers=fetch_suppliers(),
        delivery_contacts=fetch_delivery_contacts(),
        delivery_addresses=fetch_delivery_addresses()
    )

@main.route("/po/<po_id>/pdf")
def po_pdf(po_id):
    from .supabase_client import fetch_po_detail
    from datetime import datetime

    current_app.logger.info(f"📄 Route hit: PO PDF for {po_id}")

    try:
        po = fetch_po_detail(po_id)
        if not po:
            return render_template("404.html"), 404
    except Exception as e:
        flash(f"Failed to load PO: {e}", "danger")
        return redirect(url_for("main.po_list"))

    # Compute totals
    grand_total = 0
    net_total = 0
    for item in po.get("line_items", []):
        qty = item.get("quantity") or 0
        price = item.get("unit_price") or 0.0
        item["total"] = qty * price
        net_total += item["total"]
    vat_total = net_total * 0.2
    grand_total = net_total + vat_total

    # 🔥 Embed logo as base64
    logo_path = Path("app/static/img/PSS_Standard_RGB.png")
    with open(logo_path, "rb") as img_file:
        logo_base64 = base64.b64encode(img_file.read()).decode("utf-8")

    # Render template
    html = render_template(
        "po_pdf.html",
        po=po,
        net_total=net_total,
        vat_total=vat_total,
        grand_total=grand_total,
        now=datetime.now(),
        logo_base64=logo_base64,
        pdf=True
    )

    # Generate PDF
    pdf = HTML(string=html, base_url=request.root_url).write_pdf(
    stylesheets=[CSS(filename="app/static/css/pdf_style.css")]
    )

    response = make_response(pdf)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"inline; filename=PO_{po['po_number']}.pdf"
    return response
