from __future__ import annotations
import os
from pathlib import Path
from typing import List, Dict, Set, Any
from datetime import date
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

# -----------------------------
# Load environment variables
# -----------------------------
WEB_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEB_DIR.parent
HOME = Path.home()

candidates = [
    WEB_DIR / ".env",
    ROOT_DIR / ".env",
    HOME / ".rvprospector" / ".env"
]

for p in candidates:
    if p.exists():
        load_dotenv(dotenv_path=p, override=True)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_ANON_KEY = (os.getenv("SUPABASE_ANON_KEY") or "").strip()

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_ANON_KEY environment variables.")

# -----------------------------
# Constants
# -----------------------------
DEMO_LIMIT = 10
FREE_LIMIT = DEMO_LIMIT  # backward compatible
UNLIMITED_EMAILS = {"alexmomentum@gmail.com"}  # ✅ Your unlimited user
PROFILES = "profiles"
HISTORY = "history"

# -----------------------------
# Supabase client
# -----------------------------
def get_client() -> SupabaseClient:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# -----------------------------
# Profiles
# -----------------------------
def upsert_profile(sb: SupabaseClient, email: str, full_name: str | None = None) -> dict:
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Email required")

    res = sb.table(PROFILES).select("*").eq("email", email).execute()
    row = (
        res.data[0]
        if res.data
        else {
            "email": email,
            "full_name": full_name or "",
            "unlocked": False,
            "leads_used": 0,
        }
    )

    # Auto-unlock unlimited users
    if email in UNLIMITED_EMAILS and not row.get("unlocked", False):
        row["unlocked"] = True

    sb.table(PROFILES).upsert(row, on_conflict="email").execute()
    return row


def is_unlocked(sb: SupabaseClient, email: str) -> bool:
    email = (email or "").strip().lower()
    if email in UNLIMITED_EMAILS:
        return True
    res = sb.table(PROFILES).select("unlocked").eq("email", email).execute()
    return bool(res.data and res.data[0].get("unlocked", False))

# -----------------------------
# Daily Demo Limit Tracking
# -----------------------------
def get_leads_used_today(sb: SupabaseClient, email: str) -> int:
    """Counts how many parks this user has generated today (UTC)."""
    today = date.today().isoformat()
    res = (
        sb.table(HISTORY)
        .select("id")
        .eq("email", email)
        .gte("created_at", f"{today}T00:00:00Z")
        .execute()
    )
    return len(res.data or [])

def slice_by_trial(sb: SupabaseClient, email: str, requested: int) -> tuple[int, bool, int]:
    """
    Returns (allowed_today, is_unlocked, remaining_today)
    """
    unlocked = is_unlocked(sb, email)
    if unlocked:
        return (requested, True, -1)
    used_today = get_leads_used_today(sb, email)
    remaining = max(0, DEMO_LIMIT - used_today)
    allowed = min(requested, remaining)
    return (allowed, False, remaining)

# --- Legacy counters kept for backward compatibility ---

def get_leads_used(sb: SupabaseClient, email: str) -> int:
    """
    Total all-time leads counter stored in profiles.leads_used.
    (We now enforce the daily demo limit via history timestamps, but
    we keep this around so older app code can still read/update it.)
    """
    email = (email or "").strip().lower()
    res = sb.table(PROFILES).select("leads_used").eq("email", email).execute()
    return int(res.data[0]["leads_used"]) if res.data else 0


def increment_leads(sb: SupabaseClient, email: str, n: int) -> None:
    """
    Increment the legacy profiles.leads_used counter so existing
    app code that calls this will keep working.
    """
    if n <= 0:
        return
    used = get_leads_used(sb, email)
    sb.table(PROFILES).update({"leads_used": used + n}).eq("email", email).execute()

# -----------------------------
# History tracking
# -----------------------------
def fetch_history_place_ids(sb: SupabaseClient, email: str) -> Set[str]:
    res = sb.table(HISTORY).select("park_place_id").eq("email", email).execute()
    return {row["park_place_id"] for row in (res.data or []) if row.get("park_place_id")}


def record_history(sb: SupabaseClient, email: str, rows: List[Dict[str, Any]]):
    if not rows:
        return
    payload = [
        {
            "email": email,
            "park_place_id": r.get("park_place_id", ""),
            "park_name": r.get("park_name", ""),
            "phone": r.get("phone", ""),
            "website": r.get("website", ""),
            "address": r.get("address", ""),
            "city": r.get("city", ""),
            "state": r.get("state", ""),
            "zip": r.get("zip", ""),
            "source": r.get("source", "Google Places"),
            "detected_keyword": r.get("detected_keyword", ""),
            "pad_count": str(r.get("pad_count", "")),
        }
        for r in rows
    ]
    sb.table(HISTORY).upsert(payload, on_conflict="email,park_place_id").execute()
