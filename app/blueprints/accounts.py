# app/blueprints/accounts.py
from flask import Blueprint, render_template, request, jsonify
from app.supabase_client import fetch_accounts_overview, update_po_accounts_fields

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts")

@accounts_bp.route("/", methods=["GET"])
def index():
    po_list = fetch_accounts_overview()
    return render_template("accounts.html", po_list=po_list)

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
    status = 200 if result["ok"] else 500
    return jsonify(result), status
