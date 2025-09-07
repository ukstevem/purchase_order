# status_utils.py

from enum import Enum

class POStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    ISSUED = "issued"
    COMPLETE = "complete"
    CANCELLED = "cancelled"

VALID_PO_STATUSES = {status.value for status in POStatus}

# Linear flow (excluding CANCELLED, which is terminal and can be jumped to)
FLOW = [POStatus.DRAFT, POStatus.APPROVED, POStatus.ISSUED, POStatus.COMPLETE]
FLOW_INDEX = {s.value: i for i, s in enumerate(FLOW)}
TERMINALS = {POStatus.COMPLETE.value, POStatus.CANCELLED.value}

def _norm(status: str) -> str:
    return (status or "").strip().lower()

def validate_po_status(status):
    status = _norm(status)
    if status not in VALID_PO_STATUSES:
        raise ValueError(f"❌ Invalid PO status: '{status}'. Must be one of: {', '.join(VALID_PO_STATUSES)}")

def allowed_next_statuses(current_status: str) -> list[str]:
    """
    Forward-only list for dropdown.
    - If current is terminal (complete/cancelled): only itself.
    - Otherwise: same or later in FLOW, plus 'cancelled' as a terminal option.
    """
    cur = _norm(current_status)
    validate_po_status(cur)

    if cur in TERMINALS:
        return [cur]  # frozen

    start_idx = FLOW_INDEX.get(cur, 0)
    forward = [s.value for s in FLOW[start_idx:]]
    return forward + [POStatus.CANCELLED.value]  # allow cancel from any non-terminal

def is_forward_or_same(old_status: str, new_status: str) -> bool:
    """
    Guard for API: prevents moving backwards. 'cancelled' is always allowed from non-terminals.
    Terminals are frozen (only same->same).
    """
    old, new = _norm(old_status), _norm(new_status)
    validate_po_status(old); validate_po_status(new)

    if old in TERMINALS:
        return new == old  # frozen

    if new == POStatus.CANCELLED.value:
        return True  # allow cancel at any time (non-terminal)

    # both in FLOW: require index(new) >= index(old)
    return FLOW_INDEX.get(new, -1) >= FLOW_INDEX.get(old, -1)

def is_numeric_ge_1(rev) -> bool:
    try:
        return int(str(rev).strip()) >= 1
    except Exception:
        return False

def coerce_rev_on_leaving_draft(prev_rev, old_status, new_status):
    """
    If moving out of 'draft', set revision to '1' unless it's already numeric >= 1.
    Fires on any move from draft up the chain (approved/issued/complete/cancelled).
    """
    old, new = _norm(old_status), _norm(new_status)
    if old == POStatus.DRAFT.value and new != POStatus.DRAFT.value:
        return str(prev_rev).strip() if is_numeric_ge_1(prev_rev) else "1"
    return prev_rev
