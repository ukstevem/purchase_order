# app/blueprints/expediting.py

from __future__ import annotations

import math
import requests
from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    current_app,
    jsonify,
)

from app.supabase_client import (
    fetch_active_pos_from_view,
    _get_supabase_auth,
    get_headers,
)

bp = Blueprint("expediting", __name__)

PO_PAGE_SIZE = 50  # rows per page


@bp.route("/expediting", methods=["GET"])
def expediting():
    """
    Expediting overview page.
    Uses active_po_list view (latest/active POs only) + pagination.
    """

    # ---- Query params ----
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    date_from = request.args.get("from")
    date_to = request.args.get("to")

    sort = request.args.get("sort", "po_number")
    dir_ = (request.args.get("dir", "desc") or "desc").lower()

    allowed_sorts = {"po_number", "updated_at"}
    if sort not in allowed_sorts:
        sort = "po_number"
    dir_ = "asc" if dir_ == "asc" else "desc"
    order_by = f"{sort}.{dir_}"

    selected_status = (request.args.get("status", "") or "").strip()
    selected_project = (request.args.get("project", "") or "").strip()
    selected_supplier = (request.args.get("supplier", "") or "").strip()

    # ---- Fetch from Supabase view ----
    try:
        all_pos = fetch_active_pos_from_view(
            projectnumber=selected_project or None,
            supplier_name=selected_supplier or None,
            status=selected_status or None,
            date_from=date_from,
            date_to=date_to,
            order_by=order_by,
        ) or []

        total_pos = len(all_pos)
        total_pages = max(1, math.ceil(total_pos / PO_PAGE_SIZE))

        if page > total_pages:
            page = total_pages

        start_idx0 = (page - 1) * PO_PAGE_SIZE
        end_idx0 = start_idx0 + PO_PAGE_SIZE
        po_list = all_pos[start_idx0:end_idx0]

        start_index = start_idx0 + 1 if total_pos > 0 else 0
        end_index = min(end_idx0, total_pos)

        current_app.logger.debug(
            "Expediting: fetched %d purchase orders (page %d of %d)",
            total_pos,
            page,
            total_pages,
        )

        if not po_list:
            flash("No purchase orders found.", "info")

    except Exception as e:
        current_app.logger.error("Failed to load expediting data: %s", e)
        flash(f"Failed to load expediting data: {e}", "danger")
        po_list = []
        total_pos = 0
        total_pages = 1
        start_index = 0
        end_index = 0

    # ---- Pagination window (max 50 links) ----
    window = 50
    half = window // 2
    page_start = max(1, page - half)
    page_end = min(total_pages, page_start + window - 1)
    page_start = max(1, page_end - window + 1)

    return render_template(
        "expediting.html",
        po_list=po_list,
        selected_status=selected_status,
        selected_project=selected_project,
        selected_supplier=selected_supplier,
        sort=sort,
        dir=dir_,
        date_from=date_from,
        date_to=date_to,
        page=page,
        total_pages=total_pages,
        start_index=start_index,
        end_index=end_index,
        total_pos=total_pos,
        page_start=page_start,
        page_end=page_end,
    )


def _fetch_line_items_for_po(po_id: str) -> list[dict]:
    """
    Fetch active line items for a single purchase order.
    """
    base, _ = _get_supabase_auth()
    headers = get_headers(False)
    url = f"{base}/rest/v1/po_line_items"

    params = {
        "select": (
            "id,po_id,description,quantity,qty_received,"
            "exped_expected_date,exped_completed_date"
        ),
        "po_id": f"eq.{po_id}",
        "active": "is.true",
        "order": "id.asc",  # stable; adjust if you later add a line_no field
    }

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"body": resp.text}
        current_app.logger.error(
            "Supabase po_line_items error %s for po_id %s: %s",
            resp.status_code,
            po_id,
            err,
        )
    resp.raise_for_status()
    return resp.json() or []


@bp.get("/expediting/<po_id>/line-items")
def expediting_line_items(po_id: str):
    """
    JSON API: return line items for a single PO for the expediting page.
    """
    try:
        items = _fetch_line_items_for_po(po_id)
        return jsonify(items)
    except requests.RequestException as exc:
        current_app.logger.error("Failed to fetch line items for %s: %s", po_id, exc)
        return jsonify({"error": "Failed to load line items"}), 500


@bp.patch("/expediting/line-items/<item_id>")
def expediting_update_line_item(item_id: str):
    """
    PATCH a single po_line_items row.

    Accepts JSON body with any of:
      - qty_received
      - exped_expected_date  (YYYY-MM-DD or null)
      - exped_completed_date (YYYY-MM-DD or null)
    """
    data = request.get_json(silent=True) or {}
    allowed = {"qty_received", "exped_expected_date", "exped_completed_date"}
    payload = {k: v for k, v in data.items() if k in allowed}

    if not payload:
        return jsonify({"ok": False, "error": "No updatable fields"}), 400

    base, _ = _get_supabase_auth()
    url = f"{base}/rest/v1/po_line_items"
    headers = get_headers()  # includes JSON Content-Type
    params = {"id": f"eq.{item_id}"}

    resp = requests.patch(url, headers=headers, params=params, json=payload, timeout=15)
    if not resp.ok:
        try:
            err = resp.json()
        except Exception:
            err = {"body": resp.text}
        current_app.logger.error(
            "expediting_update_line_item failed (%s): %s",
            resp.status_code,
            err,
        )
        return (
            jsonify(
                {
                    "ok": False,
                    "status": resp.status_code,
                    "error": err,
                }
            ),
            500,
        )

    # Supabase usually returns a list of updated rows
    updated = None
    if resp.text:
        try:
            updated = resp.json()
        except Exception:
            updated = None

    return jsonify({"ok": True, "data": updated})
