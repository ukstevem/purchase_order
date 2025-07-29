from app.supabase_client import fetch_all_po_revisions  # You'll need to implement this

def get_max_revisions_for_po(po_number: int) -> tuple[str, str]:
    """
    Returns the highest alpha and numeric revision seen for a given PO number.

    Returns:
        (max_alpha: str, max_numeric: str)
    """


    all_revisions = fetch_all_po_revisions(po_number)
    alpha_revs = [r["current_revision"] for r in all_revisions if r["current_revision"].isalpha()]
    num_revs = [int(r["current_revision"]) for r in all_revisions if r["current_revision"].isdigit()]

    max_alpha = max(alpha_revs, default="a")
    max_numeric = str(max(num_revs)) if num_revs else "0"
    return max_alpha, max_numeric
