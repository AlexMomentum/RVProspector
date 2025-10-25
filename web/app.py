from __future__ import annotations

"""
RV Prospector — Streamlit app

Key features delivered:
- Persistent sign-in with secure cookies (no backend required) and optional Supabase auth
- Search for RV parks via Google Places API (text + near-me)
- Fast, concurrent fetching with ThreadPoolExecutor, timeouts, and caching
- Per‑user search history (Supabase table `search_history`) with CSV download
- History viewer with pagination & client-side filtering
- Robust widget keys to prevent DuplicateWidgetID errors
- Environment/Secrets bootstrap for Streamlit Cloud

Environment variables (set in Streamlit Secrets for Cloud):
- GOOGLE_PLACES_API_KEY (or GOOGLE_MAPS_API_KEY / GOOGLE_API_KEY)
- SUPABASE_URL (optional)
- SUPABASE_ANON_KEY (optional — recommended for client-side usage)
- COOKIE_SECURE = "true" on https deployments
- RVP_* knobs (see defaults below)

Supabase schema (optional but recommended):
create table if not exists search_history (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  user_email text,
  query jsonb not null,
  results_count int,
  created_at timestamptz default now()
);
create index if not exists search_history_user_created_at on search_history (user_id, created_at desc);

"""

import json
import os
import time
import uuid
import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st
import extra_streamlit_components as stx

# -----------------------------------------------------------------------------
# Tunables / Perf (override via env without redeploy)
# -----------------------------------------------------------------------------
WORKERS = int(os.getenv("RVP_WORKERS", "24"))
DEFAULT_NEAR_ME_RADIUS_M = int(os.getenv("RVP_RADIUS_M", "30000"))
TARGET_QUERY_LIMIT = int(os.getenv("RVP_QUERY_LIMIT", "8"))
PAGE_SLEEP_SECS = float(os.getenv("RVP_PAGE_SLEEP", "1.2"))
HTTP_TIMEOUT = float(os.getenv("RVP_HTTP_TIMEOUT", "5.0"))
PAGE_SIZE_HISTORY = int(os.getenv("RVP_PAGE_SIZE_HISTORY", "20"))
SEARCH_HARD_CAP = int(os.getenv("RVP_SEARCH_HARD_CAP", "100"))
COOKIE_SECURE = os.getenv("RVP_COOKIE_SECURE", "true").strip().lower() == "true"
APP_TITLE = os.getenv("RVP_APP_TITLE", "RV Prospector")

# -----------------------------------------------------------------------------
# Secrets -> env (helpful on Streamlit Cloud)
# -----------------------------------------------------------------------------

def _secrets_to_env() -> None:
    # Map alternative secret names to standard env vars
    mappings = {
        "GOOGLE_PLACES_API_KEY": [
            "GOOGLE_PLACES_API_KEY",
            "GOOGLE_MAPS_API_KEY",
            "GOOGLE_API_KEY",
        ],
        "SUPABASE_URL": ["SUPABASE_URL"],
        "SUPABASE_ANON_KEY": ["SUPABASE_ANON_KEY", "SUPABASE_KEY"],
    }
    for target, candidates in mappings.items():
        if os.getenv(target):
            continue
        for c in candidates:
            try:
                val = st.secrets[c]
            except Exception:
                val = None
            if val:
                os.environ[target] = str(val)
                break


_secrets_to_env()

# -----------------------------------------------------------------------------
# Optional Supabase client
# -----------------------------------------------------------------------------

class _Supabase:
    def __init__(self):
        self.url = os.getenv("SUPABASE_URL")
        self.key = os.getenv("SUPABASE_ANON_KEY")
        self._client = None
        if self.url and self.key:
            try:
                from supabase import create_client

                self._client = create_client(self.url, self.key)
            except Exception:
                self._client = None

    def ok(self) -> bool:
        return self._client is not None

    def upsert_history(self, row: Dict[str, Any]) -> None:
        if not self.ok():
            return
        try:
            self._client.table("search_history").insert(row).execute()
        except Exception:
            pass

    def fetch_history(self, user_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.ok():
            return []
        try:
            resp = (
                self._client.table("search_history")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return resp.data or []
        except Exception:
            return []


SUPA = _Supabase()

# -----------------------------------------------------------------------------
# Cookie manager & auth-lite (email only) — persisted login
# -----------------------------------------------------------------------------

COOKIE_NAME = "rvp_user"


def _cookie_mgr() -> stx.CookieManager:
    if "cookie_manager" not in st.session_state:
        st.session_state.cookie_manager = stx.CookieManager()
    return st.session_state.cookie_manager


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class User:
    id: str
    email: str
    name: Optional[str] = None


def _load_user_from_cookie() -> Optional[User]:
    cm = _cookie_mgr()
    raw = cm.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
        return User(id=decoded["id"], email=decoded["email"], name=decoded.get("name"))
    except Exception:
        return None


def _save_user_cookie(user: User) -> None:
    cm = _cookie_mgr()
    payload = base64.b64encode(json.dumps(user.__dict__).encode("utf-8")).decode("ascii")
    cm.set(
        COOKIE_NAME,
        payload,
        expires_at=datetime.utcnow() + timedelta(days=365),
        secure=COOKIE_SECURE,
    )


def _clear_user_cookie() -> None:
    _cookie_mgr().delete(COOKIE_NAME)


# -----------------------------------------------------------------------------
# Google Places client
# -----------------------------------------------------------------------------

PLACES_BASE = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_BASE = "https://maps.googleapis.com/maps/api/place/details/json"


def _g_api_key() -> str:
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not key:
        st.stop()
    return key


def g_places_textsearch(query: str, location: Optional[str] = None, radius_m: int = DEFAULT_NEAR_ME_RADIUS_M, max_pages: int = 2) -> List[Dict[str, Any]]:
    params = {
        "query": query,
        "key": _g_api_key(),
    }
    if location:
        params.update({"location": location, "radius": radius_m})

    results: List[Dict[str, Any]] = []
    page_token = None
    pages = 0
    while pages < max_pages and len(results) < SEARCH_HARD_CAP:
        if page_token:
            params["pagetoken"] = page_token
            time.sleep(PAGE_SLEEP_SECS)
        r = requests.get(PLACES_BASE, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
        pages += 1
    return results[:SEARCH_HARD_CAP]


def g_place_details(place_id: str, fields: Optional[str] = None) -> Dict[str, Any]:
    params = {"place_id": place_id, "key": _g_api_key()}
    if fields:
        params["fields"] = fields
    r = requests.get(DETAILS_BASE, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("result", {})


# -----------------------------------------------------------------------------
# Caching wrappers
# -----------------------------------------------------------------------------

@st.cache_data(show_spinner=False, ttl=60 * 30, max_entries=1024)
def cached_textsearch(query: str, location: Optional[str], radius_m: int, pages: int) -> List[Dict[str, Any]]:
    return g_places_textsearch(query, location, radius_m, pages)


@st.cache_data(show_spinner=False, ttl=60 * 60, max_entries=4096)
def cached_details(place_id: str, fields: Optional[str]) -> Dict[str, Any]:
    return g_place_details(place_id, fields)


# -----------------------------------------------------------------------------
# Search logic
# -----------------------------------------------------------------------------

FIELDS = "place_id,name,formatted_address,geometry/location,international_phone_number,website,business_status,opening_hours,types,rating,user_ratings_total"


def run_query(q: str, location_str: Optional[str]) -> pd.DataFrame:
    """Run a search and enrich with place details concurrently, safely."""
    radius_m = DEFAULT_NEAR_ME_RADIUS_M
    base_results = cached_textsearch(q, location_str, radius_m, pages=2)

    # Dedup by place_id
    seen = set()
    to_fetch: List[str] = []
    for r in base_results:
        pid = r.get("place_id")
        if pid and pid not in seen:
            seen.add(pid)
            to_fetch.append(pid)

    def _fetch(pid: str) -> Optional[Dict[str, Any]]:
        try:
            return cached_details(pid, FIELDS)
        except Exception:
            return None

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_fetch, pid): pid for pid in to_fetch[:SEARCH_HARD_CAP]}
        for fut in as_completed(futs):
            d = fut.result()
            if not d:
                continue
            rows.append(
                {
                    "place_id": d.get("place_id"),
                    "name": d.get("name"),
                    "address": d.get("formatted_address"),
                    "lat": (d.get("geometry", {})
                             .get("location", {})
                             .get("lat")),
                    "lng": (d.get("geometry", {})
                             .get("location", {})
                             .get("lng")),
                    "phone": d.get("international_phone_number"),
                    "website": d.get("website"),
                    "status": d.get("business_status"),
                    "rating": d.get("rating"),
                    "reviews": d.get("user_ratings_total"),
                    "types": ",".join(d.get("types", [])[:6]),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["rating", "reviews"], ascending=[False, False], na_position="last").reset_index(drop=True)
    return df


# -----------------------------------------------------------------------------
# Search history (Supabase if available, else local session fallback)
# -----------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _session_history(user_id: str) -> pd.DataFrame:
    return pd.DataFrame(columns=["created_at", "query", "results_count"]).astype({"created_at": "datetime64[ns]"})


def log_search(user: User, q: Dict[str, Any], results_count: int) -> None:
    row = {
        "user_id": user.id,
        "user_email": user.email,
        "query": q,
        "results_count": int(results_count),
        "created_at": datetime.utcnow().isoformat(),
    }
    # Supabase (best)
    SUPA.upsert_history(row)
    # Session fallback (ensures history UI works even without Supabase)
    key = f"sess_hist_{user.id}"
    df = st.session_state.get(key)
    if df is None:
        df = _session_history(user.id)
    df.loc[len(df)] = [pd.to_datetime(row["created_at"]), json.dumps(q), results_count]
    st.session_state[key] = df


def fetch_history(user: User, limit: int = 500) -> pd.DataFrame:
    # Merge supabase + session fallback
    records = SUPA.fetch_history(user.id, limit=limit)
    supa_df = pd.DataFrame(records)
    if not supa_df.empty:
        supa_df["created_at"] = pd.to_datetime(supa_df["created_at"])
        supa_df["query"] = supa_df["query"].apply(lambda x: json.dumps(x) if isinstance(x, dict) else str(x))
        supa_df = supa_df[["created_at", "query", "results_count"]]
    key = f"sess_hist_{user.id}"
    sess_df = st.session_state.get(key)
    if sess_df is None:
        sess_df = _session_history(user.id)
    df = pd.concat([supa_df, sess_df], ignore_index=True) if not supa_df.empty else sess_df.copy()
    if not df.empty:
        df = df.sort_values("created_at", ascending=False).reset_index(drop=True)
    return df.head(limit)


# -----------------------------------------------------------------------------
# UI components
# -----------------------------------------------------------------------------

def _auth_sidebar() -> Optional[User]:
    with st.sidebar:
        st.header("Account")
        user = _load_user_from_cookie()
        if user:
            st.success(f"Signed in as {user.email}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Sign out", key="btn_sign_out"):
                    _clear_user_cookie()
                    st.rerun()
            with c2:
                st.caption("Login is persisted in a cookie for 1 year.")
            return user

        st.caption("Quick sign-in — no password.")
        email = st.text_input("Email", key="auth_email")
        name = st.text_input("Name (optional)", key="auth_name")
        if st.button("Sign in", key="btn_sign_in"):
            if not email:
                st.warning("Please enter an email")
            else:
                new_user = User(id=_hash(email), email=email, name=name or None)
                _save_user_cookie(new_user)
                st.rerun()
        return None


def _history_ui(user: User) -> None:
    st.subheader("🔎 Your search history")
    df = fetch_history(user, limit=1000)
    if df.empty:
        st.info("No searches yet. Run a search to populate history.")
        return

    q_filter = st.text_input("Filter (search JSON)", key="hist_filter")
    view = df.copy()
    if q_filter:
        view = view[view["query"].str.contains(q_filter, case=False, na=False)]

    # Pagination
    page = st.number_input("Page", value=1, min_value=1, step=1, key="hist_page")
    start = (page - 1) * PAGE_SIZE_HISTORY
    end = start + PAGE_SIZE_HISTORY
    st.dataframe(view.iloc[start:end], use_container_width=True, hide_index=True)

    csv = view.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Download CSV of all history (filtered)",
        data=csv,
        file_name=f"rvp_history_{user.email.replace('@','_')}.csv",
        mime="text/csv",
        key="hist_dl_btn",
    )


# -----------------------------------------------------------------------------
# Main app
# -----------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("Find RV parks fast. Save your searches. Export results.")

    user = _auth_sidebar()

    with st.expander("⚙️ Advanced settings", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.number_input("Workers", value=WORKERS, step=4, min_value=4, key="cfg_workers", help="Concurrent detail fetchers")
        with c2:
            st.number_input("Radius (m)", value=DEFAULT_NEAR_ME_RADIUS_M, step=5000, min_value=1000, key="cfg_radius")
        with c3:
            st.number_input("Hard cap results", value=SEARCH_HARD_CAP, min_value=10, step=10, key="cfg_cap")
        with c4:
            st.number_input("HTTP timeout (s)", value=HTTP_TIMEOUT, min_value=2.0, step=0.5, key="cfg_http")
        st.caption("Changes apply on next search. You can also override with env vars RVP_*.")

    st.markdown("---")

    # Search controls
    st.subheader("🔍 Search")
    c1, c2 = st.columns([2, 1])
    with c1:
        q = st.text_input("Query", value="RV park near Phoenix AZ", key="q_input")
    with c2:
        near_me = st.checkbox("Use lat,lng (advanced)", value=False, key="near_me")
    location_str = None
    if near_me:
        lat = st.text_input("Latitude", value="33.4484", key="lat")
        lng = st.text_input("Longitude", value="-112.0740", key="lng")
        if lat and lng:
            location_str = f"{lat},{lng}"

    if st.button("Find", key="btn_find"):
        try:
            # Pull live overrides
            global WORKERS, DEFAULT_NEAR_ME_RADIUS_M, HTTP_TIMEOUT
            WORKERS = int(st.session_state["cfg_workers"]) or WORKERS
            DEFAULT_NEAR_ME_RADIUS_M = int(st.session_state["cfg_radius"]) or DEFAULT_NEAR_ME_RADIUS_M
            HTTP_TIMEOUT = float(st.session_state["cfg_http"]) or HTTP_TIMEOUT

            with st.spinner("Searching Google Places and enriching…"):
                df = run_query(q, location_str)

            st.success(f"Found {len(df)} places")
            if not df.empty:
                st.dataframe(df, use_container_width=True)
                # Per‑user CSV of results
                csv = df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "⬇️ Download results CSV",
                    csv,
                    file_name=f"rvp_results_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    key=f"dl_{uuid.uuid4()}",
                )

            # Log to history
            if user:
                q_payload = {"q": q, "location": location_str, "radius_m": DEFAULT_NEAR_ME_RADIUS_M}
                log_search(user, q_payload, len(df))
        except Exception as e:
            st.error(f"Search failed: {e}")

    st.markdown("---")

    # History viewer (requires login)
    if user:
        _history_ui(user)
    else:
        st.info("Sign in (sidebar) to enable history and CSV export of your searches.")


if __name__ == "__main__":
    main()
