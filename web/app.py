# web/app.py
from __future__ import annotations

import io
import os
import pathlib
import sys
import traceback
import uuid
from typing import Any, Dict, List

import pandas as pd
import streamlit as st
import extra_streamlit_components as stx

# -----------------------------------------------------------------------------
# Secrets -> env (for Streamlit Cloud)
# -----------------------------------------------------------------------------
def _secrets_to_env():
    mappings = {
        "GOOGLE_PLACES_API_KEY": ["GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY", "GOOGLE_API_KEY"],
        "SUPABASE_URL": ["SUPABASE_URL"],
        "SUPABASE_ANON_KEY": ["SUPABASE_ANON_KEY"],
        "SUPABASE_SERVICE_ROLE_KEY": ["SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE_KEY"],  # needed to write unlocked=True
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

# ----------- Configurable links (must exist before sidebar uses them) ----------
SIGNUP_URL = os.getenv("SIGNUP_URL", "").strip()
DONATE_URL = os.getenv("DONATE_URL", "").strip()

# -----------------------------------------------------------------------------
# Path setup so Python can find web/ and src/
# -----------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# -----------------------------------------------------------------------------
# ✅ Single, robust import of web.db
# -----------------------------------------------------------------------------
def _import_web_db():
    # Try normal import
    try:
        import web.db as dbmod
        return dbmod
    except Exception:
        pass

    # Fallback: import from file path (if 'web' isn't a package)
    import importlib.util, types
    web_dir = ROOT / "web"
    db_path = web_dir / "db.py"
    if not db_path.exists():
        raise RuntimeError(f"Could not find web/db.py at {db_path}")

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
    st.error(f"Failed to import web.db: {e.__class__.__name__}: {e}")
    st.code("".join(traceback.format_exc()))
    st.stop()

# Re-export helpers
fetch_history_place_ids = db.fetch_history_place_ids
get_client = db.get_client
increment_leads = db.increment_leads
is_unlocked = db.is_unlocked
record_history = db.record_history
upsert_profile = db.upsert_profile
slice_by_trial = db.slice_by_trial
record_signup = getattr(db, "record_signup", None)
grant_unlimited = getattr(db, "grant_unlimited", None)

# Import core AFTER paths are set
from rvprospector import core as c  # noqa: E402

# -----------------------------------------------------------------------------
# Page config + Sidebar
# -----------------------------------------------------------------------------
st.set_page_config(page_title="RV Prospector (Web)", page_icon="🗺️", layout="centered")

st.sidebar.markdown("### ❤️ Support RV Prospector")
st.sidebar.markdown("If this tool helps you, consider donating to keep it running:")
if DONATE_URL:
    st.sidebar.link_button("Donate", DONATE_URL)
else:
    st.sidebar.caption("Set DONATE_URL to show a donate button.")

# -----------------------------------------------------------------------------
# Cookie + session helpers
# -----------------------------------------------------------------------------
def _ensure_guest_cookie(cm: stx.CookieManager, cookies: Dict[str, str]) -> str:
    gid = cookies.get("rvp_guest_id")
    if not gid:
        gid = str(uuid.uuid4())
        cm.set("rvp_guest_id", gid)
    return f"guest:{gid}"

def _set_signed_in(cm: stx.CookieManager, email: str, unlocked: bool):
    st.session_state["user_key"] = email
    st.session_state["unlocked"] = bool(unlocked)
    cm.set("rvp_email", email)

def _sign_out(cm: stx.CookieManager):
    cm.delete("rvp_email")
    st.session_state.clear()
    st.rerun()

# -----------------------------------------------------------------------------
# Location helpers
# -----------------------------------------------------------------------------
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia"
}

def normalize_location(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) == 2 and s.upper() in US_STATES:
        return f"{US_STATES[s.upper()]}, USA"
    if s.title() in US_STATES.values() and "usa" not in s.lower():
        return f"{s.title()}, USA"
    return s

# -----------------------------------------------------------------------------
# Search core
# -----------------------------------------------------------------------------
def _generate_for_user(
    api_key: str,
    email: str,
    location: str,
    requested: int,
    avoid_conglomerates: bool,
    near_me: bool,
    radius_m: int = 50_000,
) -> List[Dict[str, Any]]:
    sb = get_client()
    already = fetch_history_place_ids(sb, email)
    seen: set[str] = set()
    found: List[Dict[str, Any]] = []
    checked = 0

    def emit(msg: str):
        st.session_state.setdefault("log", [])
        st.session_state["log"].append(msg)

    latlng = None
    if near_me:
        latlng = c.get_approx_location_via_ip()
        if not latlng:
            emit("[warn] Could not auto-detect location from IP; using manual location.")
            near_me = False

    for query in c.TARGET_QUERIES:
        where = "your current area" if near_me else location
        emit(f"[info] Searching '{query}' near {where}")

        token = None
        while True:
            data = c.google_text_search(
                api_key=api_key,
                query=query,
                location_bias=None if near_me else location,
                pagetoken=token,
                latlng=latlng if near_me else None,
                radius_m=radius_m,
            )
            results = data.get("results", [])
            token = data.get("next_page_token")

            for r in results:
                if checked >= c.MAX_RESULTS_TO_CHECK or len(found) >= requested:
                    break

                pid = r.get("place_id")
                if not pid or pid in seen or pid in already:
                    continue

                checked += 1
                det = c.google_place_details(api_key, pid)
                name = det.get("name", r.get("name", ""))
                website = c._sanitize_url(det.get("website", ""))
                phone = det.get("formatted_phone_number", "") or det.get("international_phone_number", "")
                addr = det.get("formatted_address", "")
                comps = {"city": "", "state": "", "zip": ""}
                for comp in det.get("address_components", []) or []:
                    types = comp.get("types", [])
                    if "locality" in types:
                        comps["city"] = comp.get("long_name", "")
                    if "administrative_area_level_1" in types:
                        comps["state"] = comp.get("short_name", "")
                    if "postal_code" in types:
                        comps["zip"] = comp.get("long_name", "")

                if avoid_conglomerates and c._is_conglomerate(name, website):
                    seen.add(pid)
                    continue

                no_booking, booking_hit, pad_count = c.check_booking_and_pads(website)
                qualifies = no_booking and (pad_count is None or pad_count >= c.PAD_MIN)
                if not qualifies:
                    seen.add(pid)
                    continue

                row = {
                    "park_place_id": pid,
                    "park_name": name,
                    "website": website,
                    "phone": phone,
                    "address": addr,
                    "city": comps["city"],
                    "state": comps["state"],
                    "zip": comps["zip"],
                    "pad_count": pad_count or "",
                    "source": "Google Places",
                }
                found.append(row)
                seen.add(pid)

            if not token or checked >= c.MAX_RESULTS_TO_CHECK or len(found) >= requested:
                break

        if len(found) >= requested:
            break

    return found

# -----------------------------------------------------------------------------
# Demo-limit modal/dialog
# -----------------------------------------------------------------------------
def _render_demo_limit_body(sb, cm):
    st.markdown("### **Daily Demo Limit Reached**")
    st.write(
        "You’ve reached your 10 demo leads for today.\n\n"
        "RV Prospector is free to try — you’ll get 10 more leads tomorrow!\n\n"
        "If you’d like unlimited access, please sign up below.\n"
        "Your support keeps this project alive ❤️"
    )
    cols = st.columns(2)
    with cols[0]:
        if SIGNUP_URL:
            st.link_button("🔓 Sign Up for Extended Use", SIGNUP_URL, use_container_width=True)
        else:
            if record_signup is not None:
                with st.form("signup_inline", border=False):
                    si_email = st.text_input("Email", placeholder="you@example.com")
                    si_name = st.text_input("Full name (optional)")
                    si_submit = st.form_submit_button("🔓 Sign Up for Extended Use")

                if si_submit and si_email and "@" in si_email:
                    try:
                        if record_signup:
                            record_signup(sb, si_email, si_name or None)
                        if grant_unlimited:
                            grant_unlimited(sb, si_email, si_name or None)
                        else:
                            sb.table("profiles").upsert(
                                {"email": si_email, "full_name": si_name or None, "unlocked": True}
                            ).execute()
                        _set_signed_in(cm, si_email, True)
                        st.success("Thanks! Your account is now Unlimited.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not save signup: {e}")
            else:
                st.caption("Set SIGNUP_URL or implement record_signup() to collect signups here.")
    with cols[1]:
        if DONATE_URL:
            st.link_button("💗 Donate", DONATE_URL, use_container_width=True)
        else:
            st.caption("Set DONATE_URL to show a donate button.")

def show_demo_limit(sb, cm):
    if hasattr(st, "modal"):
        with st.modal("Daily Demo Limit Reached", max_width=700):
            _render_demo_limit_body(sb, cm)
    else:
        @st.dialog("Daily Demo Limit Reached")
        def _dlg():
            _render_demo_limit_body(sb, cm)
        _dlg()

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
def main():
    st.markdown("<h1>🗺️ RV Prospector</h1>", unsafe_allow_html=True)
    st.caption("Find RV parks without online booking — Demo gives you 10 new leads per day.")

    # Exactly ONE CookieManager
    cm = stx.CookieManager(key="rvp_cookies")

    sb = get_client()
    st.session_state.setdefault("log", [])

    # Fetch cookies ONCE per run
    cookies = cm.get_all()
    if cookies is None:
        st.stop()

    # Initialize identity
    if "user_key" not in st.session_state:
        saved_email = cookies.get("rvp_email")
        if saved_email:
            prior = bool(st.session_state.get("unlocked"))
            try:
                unlocked_db = bool(is_unlocked(sb, saved_email))
            except Exception:
                unlocked_db = False
            _set_signed_in(cm, saved_email, prior or unlocked_db)  # don't downgrade
        else:
            st.session_state["user_key"] = _ensure_guest_cookie(cm, cookies)
            st.session_state["unlocked"] = False

    # ------------------------ Account box ------------------------
    with st.expander("🔐 Sign In / Account", expanded=False):
        is_guest = str(st.session_state["user_key"]).startswith("guest:")

        if is_guest:
            with st.form("login", border=False):
                email = st.text_input("Email (optional, for saving your history)")
                full_name = st.text_input("Full Name (optional)")
                submitted = st.form_submit_button("Sign In")

            if submitted and email and "@" in email:
                try:
                    upsert_profile(get_client(), email, full_name or None)
                    unlocked_now = bool(is_unlocked(get_client(), email))
                    _set_signed_in(cm, email, unlocked_now)
                    st.success(f"✅ Signed in as {email} ({'Unlimited' if unlocked_now else 'Demo user'})")
                    st.rerun()
                except Exception as e:
                    st.warning(f"Login issue: {e}")
        else:
            user_email = str(st.session_state["user_key"])
            session_unlocked = bool(st.session_state.get("unlocked"))
            try:
                db_unlocked = bool(is_unlocked(get_client(), user_email))
            except Exception:
                db_unlocked = False
            current_unlocked = session_unlocked or db_unlocked
            _set_signed_in(cm, user_email, current_unlocked)

            st.write(
                f"Signed in as **{user_email}** "
                f"({'Unlimited' if st.session_state.get('unlocked') else 'Demo user'})"
            )

            if not st.session_state.get("unlocked"):
                if st.button("Activate Unlimited"):
                    try:
                        upsert_profile(get_client(), user_email, None)
                        if grant_unlimited:
                            grant_unlimited(get_client(), user_email, None)
                        else:
                            get_client().table("profiles").upsert(
                                {"email": user_email, "unlocked": True}
                            ).execute()
                        _set_signed_in(cm, user_email, True)
                        st.success("Unlimited activated for your account.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to activate unlimited: {e}")

            if st.button("Sign out"):
                _sign_out(cm)

    # API key (from env/secrets)
    api_key = c.load_api_key()
    if not api_key:
        st.error("Server misconfigured: missing API key.")
        st.stop()

    st.divider()

    # Controls
    col1, col2 = st.columns(2)
    with col1:
        near_me = st.checkbox("Use my current area", value=True)
    with col2:
        avoid_conglom = st.checkbox("Avoid chains/conglomerates", value=True)

    st.caption("🌎 Enter a location to override your area.")
    raw_loc = st.text_input("Location (optional)", placeholder="e.g. Phoenix, AZ or UT")
    location = normalize_location(raw_loc)
    use_near_me = not bool(location)

    requested = st.number_input("How many parks to find?", 1, 200, 10)

    if st.button("🚀 Find RV Parks"):
        user_key = st.session_state["user_key"]
        unlocked = bool(st.session_state.get("unlocked"))

        # Demo/Unlimited slicing
        allowed, is_unlim, remaining = (requested, True, -1) if unlocked else slice_by_trial(
            sb, user_key, int(requested)
        )

        if not is_unlim and allowed <= 0:
            show_demo_limit(sb, cm)
            st.stop()

        with st.status("Searching for parks...", expanded=True) as status:
            st.session_state["log"] = []
            rows = _generate_for_user(
                api_key=api_key,
                email=user_key,
                location=location or "",
                requested=int(allowed),
                avoid_conglomerates=avoid_conglom,
                near_me=use_near_me,
            )
            record_history(sb, user_key, rows)
            if not is_unlim and not str(user_key).startswith("guest:"):
                increment_leads(sb, user_key, len(rows))
            status.update(label="✅ Done", state="complete")

        if not rows:
            st.info("No new parks found.")
            st.stop()

        # ---------------------- Clean display ----------------------
        df = pd.DataFrame(rows)
        if "park_place_id" in df.columns:
            df = df.drop(columns=["park_place_id"])

        df.insert(0, "#", range(1, len(df) + 1))

        if "website" in df.columns and "park_name" in df.columns:
            df["park_name"] = df.apply(
                lambda x: f"[{x['park_name']}]({x['website']})" if x["website"] else x["park_name"],
                axis=1,
            )
            df = df.drop(columns=["website"])

        st.subheader(f"Results ({len(df)})")
        try:
            st.markdown(df.to_markdown(index=False), unsafe_allow_html=True)
        except Exception:
            st.dataframe(df, use_container_width=True, hide_index=True)

        # CSV download
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        st.download_button("⬇️ Download CSV", buf.getvalue(), "rv_parks.csv", "text/csv")

        with st.expander("Run Log"):
            st.code("\n".join(st.session_state.get("log", [])))

if __name__ == "__main__":
    main()
