# web/app.py
from __future__ import annotations
import io, os, pathlib, sys, traceback, uuid, time
from typing import Any, Dict, List
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
import extra_streamlit_components as stx
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================================================
# Tunables / Perf
# =============================================================================
WORKERS = int(os.getenv("RVP_WORKERS", "20"))
DEFAULT_NEAR_ME_RADIUS_M = int(os.getenv("RVP_RADIUS_M", "25000"))
TARGET_QUERY_LIMIT = int(os.getenv("RVP_QUERY_LIMIT", "999"))
PAGE_SLEEP_SECS = float(os.getenv("RVP_PAGE_SLEEP", "2.2"))
PAGE_SIZE_HISTORY = 20
SEARCH_HARD_CAP = 100
PAD_HTTP_TIMEOUT = float(os.getenv("RVP_PAD_HTTP_TIMEOUT", "5.0"))
COOKIE_SECURE = os.getenv("RVP_COOKIE_SECURE", "false").strip().lower() == "true"
COOKIE_SAMESITE = os.getenv("RVP_COOKIE_SAMESITE", "Lax")

# =============================================================================
# Secrets -> env
# =============================================================================
def _secrets_to_env():
    mappings = {
        "GOOGLE_PLACES_API_KEY": ["GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY", "GOOGLE_API_KEY"],
        "SUPABASE_URL": ["SUPABASE_URL"],
        "SUPABASE_ANON_KEY": ["SUPABASE_ANON_KEY"],
        "SUPABASE_SERVICE_ROLE_KEY": ["SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE_KEY"],
        "SIGNUP_URL": ["SIGNUP_URL"],
        "DONATE_URL": ["DONATE_URL"],
    }
    for env_name, candidates in mappings.items():
        if os.getenv(env_name):
            continue
        for key in candidates:
            try:
                val = st.secrets.get(key)
            except Exception:
                val = None
            if val:
                os.environ[env_name] = str(val)
                break
_secrets_to_env()

SIGNUP_URL = os.getenv("SIGNUP_URL", "").strip()
DONATE_URL = os.getenv("DONATE_URL", "").strip()

# =============================================================================
# Imports / Paths
# =============================================================================
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

def _import_web_db():
    try:
        import web.db as dbmod
        return dbmod
    except Exception:
        pass
    import importlib.util, types
    db_path = ROOT / "web" / "db.py"
    if not db_path.exists():
        raise RuntimeError("Missing web/db.py")
    spec = importlib.util.spec_from_file_location("web.db", db_path)
    dbmod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("web", types.ModuleType("web"))
    sys.modules["web.db"] = dbmod
    assert spec.loader is not None
    spec.loader.exec_module(dbmod)
    return dbmod

try:
    db = _import_web_db()
except Exception as e:
    st.error(f"DB import failed: {e}")
    st.stop()

# re-exports
get_client = db.get_client
is_unlocked = db.is_unlocked
upsert_profile = db.upsert_profile
record_history = db.record_history
increment_leads = db.increment_leads
slice_by_trial = db.slice_by_trial
record_signup = getattr(db, "record_signup", None)
grant_unlimited = getattr(db, "grant_unlimited", None)
list_history_rows = getattr(db, "list_history_rows", None)
list_history_all = getattr(db, "list_history_all", None)

from rvprospector import core as c  # type: ignore

# =============================================================================
# UI helpers
# =============================================================================
st.set_page_config(page_title="RV Prospector", page_icon="🗺️", layout="centered")
st.sidebar.markdown("### ❤️ Support RV Prospector")
if DONATE_URL:
    st.sidebar.link_button("Donate", DONATE_URL)
else:
    st.sidebar.caption("Set DONATE_URL to show a donate button.")

# Cookie helpers --------------------------------------------------------------
def _cm_set(cm, key, value):
    exp = datetime.utcnow() + timedelta(days=180)
    try:
        cm.set(key, value, expires_at=exp, key=key, path="/", secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    except Exception:
        cm.set(key, value)

def _ensure_guest_cookie(cm, cookies):
    gid = cookies.get("rvp_guest_id")
    if not gid:
        gid = str(uuid.uuid4())
        _cm_set(cm, "rvp_guest_id", gid)
    return f"guest:{gid}"

def _sign_out(cm):
    try:
        cm.delete("rvp_email")
    except Exception:
        _cm_set(cm, "rvp_email", "")
    st.session_state.clear()
    st.rerun()

# =============================================================================
# Cached Google calls
# =============================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def _cached_place_details(api_key, pid):
    return c.google_place_details(api_key, pid)

@st.cache_data(ttl=600, show_spinner=False)
def _cached_text_search(api_key, query, location_bias, pagetoken, latlng, radius_m):
    return c.google_text_search(
        api_key=api_key, query=query, location_bias=location_bias,
        pagetoken=pagetoken, latlng=latlng, radius_m=radius_m
    )

# =============================================================================
# Search logic
# =============================================================================
def _generate_for_user(api_key, email, location, requested, avoid_conglomerates, near_me, radius_m):
    sb = get_client()
    seen, found = set(), []
    radius_m = int(radius_m or DEFAULT_NEAR_ME_RADIUS_M)
    emit = lambda m: st.session_state.setdefault("log", []).append(m)

    latlng = c.get_approx_location_via_ip() if near_me else None
    OTA = ("booking.com","expedia","hotels.com","koa.com","goodsam.com",
           "campendium","reserveamerica","hipcamp","rvshare","roverpass","recreation.gov")

    def eval_place(pid, name_hint):
        try:
            det = _cached_place_details(api_key, pid)
            name = det.get("name", name_hint)
            site = c._sanitize_url(det.get("website", ""))
            phone = det.get("formatted_phone_number") or det.get("international_phone_number") or ""
            if not site and not phone:
                return None
            if any(x in site.lower() for x in OTA):
                return None
            if avoid_conglomerates and c._is_conglomerate(name, site):
                return None
            try:
                no_booking, _, pads = c.check_booking_and_pads(site, timeout_sec=PAD_HTTP_TIMEOUT)
            except TypeError:
                no_booking, _, pads = c.check_booking_and_pads(site)
            if not no_booking:
                return None
            return {
                "park_place_id": pid,
                "park_name": name,
                "website": site,
                "phone": phone,
                "address": det.get("formatted_address",""),
                "city": c.get_component(det, "locality"),
                "state": c.get_component(det, "administrative_area_level_1"),
                "zip": c.get_component(det, "postal_code"),
                "pad_count": pads or "",
                "source": "Google Places",
            }
        except Exception as e:
            emit(f"[warn] {pid}: {e}")
            return None

    for idx, query in enumerate(c.TARGET_QUERIES):
        if idx >= TARGET_QUERY_LIMIT or len(found) >= requested: break
        emit(f"[info] Searching '{query}' near {location or 'your area'}")
        token = None
        while True:
            data = _cached_text_search(api_key, query, None if near_me else location, token, latlng, radius_m)
            results, token = data.get("results", []), data.get("next_page_token")
            if not results: break
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = [ex.submit(eval_place, r["place_id"], r.get("name","")) for r in results if r.get("place_id") not in seen]
                for fut in as_completed(futs):
                    row = fut.result()
                    if row:
                        seen.add(row["park_place_id"])
                        found.append(row)
                        if len(found) >= requested: break
            if not token or len(found) >= requested: break
            time.sleep(PAGE_SLEEP_SECS)
    emit(f"[done] Found {len(found)} new parks.")
    return found

# =============================================================================
# Pager helpers
# =============================================================================
def _pager(page, has_next):
    def go_prev(): st.session_state["__hist_page"] = max(1, page - 1)
    def go_next(): st.session_state["__hist_page"] = page + 1
    st.divider()
    c1, c2, c3 = st.columns([1,2,1])
    with c1:
        st.button("◀ Prev", disabled=(page<=1), on_click=go_prev, use_container_width=True)
    with c2:
        st.markdown(f"<div style='text-align:center;padding:6px 0'>Page <b>{page}</b></div>", unsafe_allow_html=True)
    with c3:
        st.button("Next ▶", disabled=(not has_next), on_click=go_next, use_container_width=True)

# =============================================================================
# Main
# =============================================================================
def main():
    st.title("🗺️ RV Prospector")
    st.caption("Find RV parks without online booking — 10 new demo leads per day.")

    cm = stx.CookieManager(key="rvp_cookies")
    cookies = cm.get_all() or {}
    if "user_key" not in st.session_state:
        email = cookies.get("rvp_email") or _ensure_guest_cookie(cm, cookies)
        st.session_state["user_key"] = email
        st.session_state["unlocked"] = bool(is_unlocked(get_client(), email))

    user_key = st.session_state["user_key"]
    unlocked = bool(st.session_state["unlocked"])
    sb = get_client()

    # ---------- HISTORY ----------
    with st.expander("📜 View My Search History", expanded=False):
        st.session_state.setdefault("__hist_page", 1)
        page = st.session_state["__hist_page"]
        offset = (page - 1) * PAGE_SIZE_HISTORY
        try:
            rows_plus = list_history_rows(sb, user_key, limit=PAGE_SIZE_HISTORY + 1, offset=offset)
        except Exception as e:
            rows_plus = []
            st.error(f"History load error: {e}")
        rows, has_next = rows_plus[:PAGE_SIZE_HISTORY], len(rows_plus) > PAGE_SIZE_HISTORY
        if rows:
            df = pd.DataFrame(rows)
            if {"park_name","website"}.issubset(df.columns):
                df["park_name"] = df.apply(lambda r: f"<a href='{r['website']}' target='_blank'>{r['park_name']}</a>", axis=1)
            st.markdown(df.to_html(escape=False, index=False), unsafe_allow_html=True)
            _pager(page, has_next)
            all_rows = list_history_all(sb, user_key)
            st.download_button("⬇️ Download My Entire History (CSV)",
                               data=pd.DataFrame(all_rows).to_csv(index=False),
                               file_name="rvprospector_history.csv",
                               mime="text/csv", use_container_width=True)
        else:
            st.caption("No history yet.")

    # ---------- SEARCH ----------
    col1, col2 = st.columns(2)
    with col1: near_me = st.checkbox("Use my current area", value=True)
    with col2: avoid_conglom = st.checkbox("Avoid chains/conglomerates", value=True)
    st.caption("🌎 Enter a location to override your area.")
    location = st.text_input("Location (optional)", "").strip()
    use_near_me = not bool(location)
    requested = st.number_input("How many parks? (max 100)", 1, SEARCH_HARD_CAP, 10)

    if st.button("🚀 Find RV Parks"):
        allowed, is_unlim, _ = (requested, True, -1) if unlocked else slice_by_trial(sb, user_key, requested)
        if not is_unlim and allowed <= 0:
            st.warning("Demo limit reached.")
            st.stop()
        st.session_state["log"] = []
        with st.status("Searching...", expanded=True) as s:
            rows = _generate_for_user(c.load_api_key(), user_key, location, int(allowed),
                                      avoid_conglom, use_near_me, DEFAULT_NEAR_ME_RADIUS_M)
            record_history(sb, user_key, rows)
            if not is_unlim and "guest" not in user_key:
                increment_leads(sb, user_key, len(rows))
            s.update(label="✅ Done", state="complete")
        if not rows:
            st.info("No parks found.")
            return
        df = pd.DataFrame(rows)
        df.insert(0, "#", range(1, len(df)+1))
        st.dataframe(df[["#","park_name","phone","address","city","state","zip"]],
                     use_container_width=True, hide_index=True)
        st.download_button("⬇️ Download CSV", pd.DataFrame(rows).to_csv(index=False),
                           "rv_parks.csv", "text/csv")
        with st.expander("Run Log"): st.code("\n".join(st.session_state.get("log", [])))

if __name__ == "__main__":
    main()
