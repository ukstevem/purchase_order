def get_next_revision(current: str) -> str:
    """
    Returns the next revision string given the current one.
    Draft revisions are lowercase letters: 'a' → 'b' → 'c' ...
    Released revisions are numbers: '1' → '2' → '3' ...
    Raises ValueError for unsupported formats.
    """
    rev = current.strip()

    # Case 1: Alphabetic draft revisions
    if len(rev) == 1 and rev.isalpha() and rev.islower():
        if rev == 'Z':
            raise ValueError("Revision limit reached (Z)")
        return chr(ord(rev) + 1)

    # Case 2: Numeric released revisions
    if rev.isdigit():
        return str(int(rev) + 1)

    # Unsupported format
    raise ValueError(f"Invalid revision format: '{current}'")


def update_revision_and_status(current_rev: str, current_status: str, new_status: str) -> str:
    """
    Determines the next revision based on current revision and a change in status.
    Converts alpha revision to '1' if status becomes 'approved'.
    """
    if current_status != new_status:
        if new_status == "approved" and current_rev.isalpha() and current_rev.islower():
            return "1"
    return get_next_revision(current_rev)


from app.utils.status_utils import POStatus
from app.utils.revision import get_next_revision

TERMINAL_STATUSES = {
    POStatus.ISSUED.value,
    POStatus.COMPLETE.value,
    POStatus.CANCELLED.value
}


def compute_updated_revision(current_rev: str, current_status: str, new_status: str) -> str:
    """
    Simplified logic:
    - 'a' → '1' when approved
    - No change if moving to cancelled or complete
    - Once approved, all further edits increment numeric revision
    - Cannot revert to draft from approved
    """
    current_rev = current_rev.strip().lower()
    current_status = current_status.lower()
    new_status = new_status.lower()

    if new_status in TERMINAL_STATUSES:
        return current_rev  # no rev change

    # Initial transition from draft → approved
    if current_status == "draft" and new_status == "approved":
        return "1"

    # After approved: revision must be numeric and incremented
    if current_rev.isdigit():
        return str(int(current_rev) + 1)

    # Stay in alpha series while still drafting
    if current_status == "draft" and new_status == "draft":
        return get_next_revision(current_rev)

    return current_rev  # fallback
