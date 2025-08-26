import json
import os
from functools import lru_cache
from flask import current_app

@lru_cache(maxsize=1)
def load_certs_table():
    # Resolve path relative to app root
    base = os.path.dirname(current_app.root_path) if current_app else os.getcwd()
    path = os.path.join(base, "app", "data", "certs_table.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
