# web/lead_sync.py
from __future__ import annotations
import os
import requests


def subscribe_mailchimp(email: str, phone: str = "") -> bool:
    """
    Optional Mailchimp subscriber. Returns True on success; otherwise False.
    Requires env:
      MAILCHIMP_API_KEY
      MAILCHIMP_SERVER_PREFIX  (e.g., "us21")
      MAILCHIMP_LIST_ID
    """
    api_key = os.getenv("MAILCHIMP_API_KEY", "")
    server = os.getenv("MAILCHIMP_SERVER_PREFIX", "")
    list_id = os.getenv("MAILCHIMP_LIST_ID", "")

    if not (api_key and server and list_id and email):
        # Not configured → treat as success only if you prefer; here we require config.
        return False

    url = f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members"
    data = {
        "email_address": email,
        "status": "subscribed",
        "merge_fields": {"PHONE": phone},
    }
    try:
        resp = requests.post(url, auth=("anystring", api_key), json=data, timeout=10)
        return resp.status_code in (200, 201)
    except Exception:
        return False
