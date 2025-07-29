# status_utils.py

from enum import Enum

class POStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    ISSUED = "issued"
    COMPLETE = "complete"
    CANCELLED = "cancelled"

VALID_PO_STATUSES = {status.value for status in POStatus}

def validate_po_status(status):
    """
    Validates the given purchase order status against allowed values.

    Args:
        status (str): The status to validate.

    Raises:
        ValueError: If the status is not valid.
    """
    if status not in VALID_PO_STATUSES:
        raise ValueError(f"❌ Invalid PO status: '{status}'. Must be one of: {', '.join(VALID_PO_STATUSES)}")
