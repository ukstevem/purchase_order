from datetime import datetime
from markupsafe import Markup, escape
from decimal import Decimal, InvalidOperation
import zoneinfo

def format_date(value, fmt="%d %b %Y", tz="Europe/London"):
    """
    Accepts:
      - Python datetime (aware or naive)
      - 'YYYY-MM-DD'
      - ISO 8601 timestamp like '2025-09-22T08:14:55.123456Z' or '+00:00'
    Renders in the specified timezone (default: Europe/London).
    """
    if not value:
        return ""

    tzinfo = zoneinfo.ZoneInfo(tz)

    # Already a datetime?
    if isinstance(value, datetime):
        dt = value
        # If naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
        return dt.astimezone(tzinfo).strftime(fmt)

    s = str(value)

    # Try full ISO first (handles 'Z' or '+00:00')
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
        return dt.astimezone(tzinfo).strftime(fmt)
    except Exception:
        pass

    # Try plain YYYY-MM-DD
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")  # tolerate extra time part
        # Treat plain dates as midnight UTC
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
        return dt.astimezone(tzinfo).strftime(fmt)
    except Exception:
        return s  # last-resort: show original



def nl2br(value):
    if value is None:
        return ""
    return Markup("<br>").join(escape(value).splitlines())


def accounting(value, symbol="£", dash_for_zero=False):
    """
    Accounting-style currency:
    - thousands separators, 2 dp
    - negatives in parentheses
    - optional em dash for zero (dash_for_zero=True)
    Robust to None/strings like '1,234.5' or '£123'.
    """
    if value is None or value == "":
        n = Decimal("0")
    else:
        try:
            s = str(value).strip().replace(",", "").replace("£", "")
            n = Decimal(s)
        except (InvalidOperation, ValueError, TypeError):
            return ""

    if dash_for_zero and n == 0:
        return "—"

    abs_str = f"{abs(n):,.2f}"
    out = f"{symbol}{abs_str}" if symbol else abs_str
    return f"({out})" if n < 0 else out


def accounting_number(value, dash_for_zero=False):
    """
    Returns a number string only (no currency symbol):
      1,234.56   or   (1,234.56)   or   — (if dash_for_zero=True and value==0)
    """
    if value is None or value == "":
        n = Decimal("0")
    else:
        try:
            s = str(value).strip().replace(",", "").replace("£", "")
            n = Decimal(s)
        except (InvalidOperation, ValueError, TypeError):
            return ""

    if dash_for_zero and n == 0:
        return "—"

    abs_str = f"{abs(n):,.2f}"
    return f"({abs_str})" if n < 0 else abs_str

