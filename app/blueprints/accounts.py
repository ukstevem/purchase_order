# app/blueprints/accounts.py
from flask import Blueprint, render_template, request, jsonify, url_for, redirect
from app.supabase_client import (
    fetch_accounts_overview,
    update_po_accounts_fields,
    fetch_projects,
    fetch_suppliers,
)

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts")


@accounts_bp.route("/", methods=["GET"])
def index():
    """
    Accounts page with filters via query params:
      - completed: 'all' | 'only' | 'exclude'  (defaults 'all')
      - project: projectnumber string          (defaults '')
      - supplier: supplier_name string         (defaults '')
    """
    # read filters from URL
    completed = (request.args.get("completed", "all") or "all").strip().lower()
    selected_project = (request.args.get("project", "") or "").strip()
    selected_supplier = (request.args.get("supplier", "") or "").strip()

    po_list = fetch_accounts_overview() or []

    # build dropdown options from the rows you actually have
    project_options = sorted({str(r.get("projectnumber")).strip()
                              for r in po_list if r.get("projectnumber")})
    supplier_options = sorted({str(r.get("supplier_name")).strip()
                               for r in po_list if r.get("supplier_name")})

    def as_str(x):
        return "" if x is None else str(x).strip()

    def truthy(x):
        return str(x).lower() in {"1", "true", "t", "yes", "y"}

    def is_completed(row):
        """
        Prefer 'acc_complete' if present; otherwise infer from status.
        """
        if "acc_complete" in row and row.get("acc_complete") is not None:
            return truthy(row.get("acc_complete"))
        status = as_str(row.get("status")).lower()
        # treat these statuses as 'completed' for the accounts view
        return status in {"issued", "complete", "completed", "closed", "paid"}

    def passes_completed(row):
        if completed == "only":
            return is_completed(row)
        if completed == "exclude":
            return not is_completed(row)
        return True  # 'all'

    def passes_project(row):
        if not selected_project:
            return True
        return as_str(row.get("projectnumber")) == selected_project

    def passes_supplier(row):
        if not selected_supplier:
            return True
        return as_str(row.get("supplier_name")) == selected_supplier

    filtered = [r for r in po_list if passes_completed(r) and passes_project(r) and passes_supplier(r)]

    return render_template(
        "accounts.html",
        po_list=filtered,
        # pass filter state + options to template
        completed=completed,
        selected_project=selected_project,
        selected_supplier=selected_supplier,
        project_options=project_options,
        supplier_options=supplier_options,
    )



@accounts_bp.route("/update", methods=["POST"])
def update():
    """
    JSON body: { "id": "<po_uuid>", "acc_complete": true/false?, "invoice_reference": "str?" }
    Only fields present are updated.
    """
    data = request.get_json(silent=True) or {}
    po_id = data.get("id")
    if not po_id:
        return jsonify({"ok": False, "error": "Missing id"}), 400

    # Pass through only present keys
    acc_complete = data["acc_complete"] if "acc_complete" in data else None
    invoice_reference = data["invoice_reference"] if "invoice_reference" in data else None

    result = update_po_accounts_fields(po_id, acc_complete, invoice_reference)
    status = 200 if result.get("ok") else 500
    return jsonify(result), status
