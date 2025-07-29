import requests
from app.supabase_client import get_headers
from flask import current_app

def get_project_id_by_number(projectnumber):
    url = f"{current_app.config['SUPABASE_URL']}/rest/v1/projects"
    params = {
        "projectnumber": f"eq.{projectnumber}",
        "select": "id"
    }
    resp = requests.get(url, headers=get_headers(), params=params)
    resp.raise_for_status()
    data = resp.json()
    print("🧪 project ID lookup result:", data)  # Add this
    return data[0]["id"] if data else None
