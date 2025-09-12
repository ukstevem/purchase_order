from datetime import datetime
from markupsafe import Markup, escape
from decimal import Decimal, InvalidOperation

def format_date(value, fmt="%d %b %Y"):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime(fmt)
    try:
        # assuming incoming format is YYYY-MM-DD
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.strftime(fmt)
    except Exception:
        return value


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

