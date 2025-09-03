from app.utils.status_utils import POStatus

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
        if rev == 'z':
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

TERMINAL_STATUSES = {
    POStatus.ISSUED.value,
    POStatus.COMPLETE.value,
    POStatus.CANCELLED.value
}

def compute_updated_revision(current_rev: str, current_status: str, new_status: str) -> str:
    """
    Minimal rule set:
    - Draft -> Approved: force revision to '1' once.
    - Otherwise: NEVER change revision automatically.
    """
    current_rev = (current_rev or "").strip()
    current_status = (current_status or "").strip().lower()
    new_status = (new_status or "").strip().lower()

    # Only one automatic conversion in the lifecycle
    if current_status == "draft" and new_status == "approved":
        # if already numeric, keep it; if alpha, convert to '1'
        return "1" if (len(current_rev) == 1 and current_rev.isalpha() and current_rev.islower()) else current_rev

    # No auto-bumps in any other case
    return current_rev
