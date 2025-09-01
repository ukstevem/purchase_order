# app/utils/pdf_archive.py
import os
import logging
from pathlib import Path

def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)

def save_pdf_archive(pdf_bytes: bytes, relative_dir: str, filename: str):
    """
    Writes to NETWORK_ARCHIVE_DIR / relative_dir / filename.
    Controlled by env only:
      - NETWORK_ARCHIVE_DIR (default /app/output/archive)
      - SAVE_PDF_ON_DOWNLOAD ("1"/"true"/"yes" to enable; default on)
    Returns the final Path or None on skip/failure.
    """
    if os.environ.get("SAVE_PDF_ON_DOWNLOAD", "1").lower() not in {"1","true","yes"}:
        return None

    root = os.environ.get("NETWORK_ARCHIVE_DIR", "/app/output/archive")
    dest = Path(root) / relative_dir / filename
    try:
        _atomic_write_bytes(dest, pdf_bytes)
        logging.info(f"Archived PDF to {dest}")
        return dest
    except Exception as e:
        logging.exception(f"Failed to archive PDF to {dest}: {e}")
        return None
