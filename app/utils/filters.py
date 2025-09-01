from datetime import datetime

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
