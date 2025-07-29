def parse_po_form(form):
    """Parses the shared PO form and returns:
    - metadata: delivery info fields
    - line_items: list of dicts
    """
    test_cert_required = bool(form.get("test_cert_required"))

    metadata = {
        "project_id": form["project_id"],
        "supplier_id": form["supplier_id"],
        "delivery_terms": form["delivery_terms"],
        "delivery_date": form["delivery_date"],
        "test_certificates_required": test_cert_required,
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
        if desc.lower() == "material test certificates":
            if not test_cert_required:
                continue  # remove if not required
            # If required, we'll handle below so skip here too
            continue

        line_items.append({
            "description": desc,
            "quantity": float(quantities[i]),
            "unit": units[i],
            "unit_price": float(unit_prices[i]),
            "currency": "GBP",
            "active": True
        })

    # Inject test cert line item if needed
    if test_cert_required:
        line_items.append({
            "description": "Material Test Certificates",
            "quantity": float(1.0),
            "unit": "Set",
            "unit_price": 0.0,
            "currency": "GBP",
            "active": True
        })

    return metadata, line_items
