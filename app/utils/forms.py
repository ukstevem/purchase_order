def _to_float(val) -> float:
    if val is None:
        return 0.0
    s = str(val).strip()
    if not s:
        return 0.0
    # remove currency symbols and thousands separators
    s = s.replace("£", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0  # or raise, if you prefer strictness

def parse_po_form(form):
    """Parses the shared PO form and returns:
    - metadata: delivery info fields
    - line_items: list of dicts
    """

    raw = (form.get("test_cert_required") or "").strip().lower()
    test_cert_required = raw in {"1", "true", "on", "yes", "y"}

    metadata = {
        "project_id": form["project_id"],
        "supplier_id": form["supplier_id"],
        "delivery_terms": form["delivery_terms"],
        "delivery_date": form["delivery_date"],
        "test_certificates_required": test_cert_required,
        "supplier_reference_number": (form.get("supplier_reference_number") or "").strip() or None,
    }

    # Build line items
    descriptions = form.getlist("description[]")
    quantities = form.getlist("quantity[]")
    units = form.getlist("unit[]")
    unit_prices = form.getlist("unit_price[]")

    line_items = []
    for i in range(len(descriptions)):
        desc = descriptions[i].strip()
        if not desc:
            continue  # skip blank lines

        # Avoid adding duplicate Test Certificates if it already exists
        if desc.lower() == "test certificates":
            if not test_cert_required:
                continue  # remove if not required
            # If required, we'll handle below so skip here too
            continue

        line_items.append({
            "description": desc,
            "quantity": _to_float(quantities[i]),
            "unit": units[i],
            "unit_price": _to_float(unit_prices[i]),
            "currency": "GBP",
            "active": True
        })

    # Inject test cert line item if needed
    if test_cert_required:
        line_items.append({
            "description": "Test Certificates",
            "quantity": float(1.0),
            "unit": "Set",
            "unit_price": 0.0,
            "currency": "GBP",
            "active": True
        })

    # Inject delivery date default
    delivery_date = metadata.get("delivery_date")
    if delivery_date:
        for li in line_items:
            # Don't overwrite a per-line value if you add one later
            if not li.get("exped_expected_date"):
                li["exped_expected_date"] = delivery_date

    return metadata, line_items