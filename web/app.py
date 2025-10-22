from __future__ import annotations
import io
import pathlib
import sys
import uuid
from typing import Any, Dict, List

import pandas as pd
import streamlit as st
import extra_streamlit_components as stx

# --------------------------------------------------------------------------------------
# Imports setup
# --------------------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rvprospector import core as c  # noqa: E402
from web.db import (
    fetch_history_place_ids,
    get_client,
    increment_leads,
    is_unlocked,
    record_history,
    upsert_profile,
    slice_by_trial,
)  # noqa: E402

# --------------------------------------------------------------------------------------
# Page Config + Sidebar
# --------------------------------------------------------------------------------------
st.set_page_config(page_title="RV Prospector (Web)", page_icon="🗺️", layout="centered")

st.sidebar.markdown("### ❤️ Support RV Prospector")
st.sidebar.markdown("If this tool helps you, consider donating to keep it running:")
st.sidebar.link_button("Donate via PayPal", "https://www.paypal.com/donate?hosted_button_id=YOUR_BUTTON_ID")
st.sidebar.link_button("Buy Me a Coffee ☕", "https://www.buymeacoffee.com/YOUR_HANDLE")

# --------------------------------------------------------------------------------------
# Cookie Tracking
# --------------------------------------------------------------------------------------
def _cookie_mgr():
    if "cookie_manager" not in st.session_state:
        st.session_state["cookie_manager"] = stx.CookieManager()
    return st.session_state["cookie_manager"]

def _guest_key_from_cookie() -> str:
    cm = _cookie_mgr()
    cookies = cm.get_all()
    if cookies is None:
        st.stop()
    gid = cookies.get("rvp_guest_id")
    if not gid:
        gid = str(uuid.uuid4())
        cm.set("rvp_guest_id", gid)
    return f"guest:{gid}"

# --------------------------------------------------------------------------------------
# Demo Limit Popup
# --------------------------------------------------------------------------------------
@st.dialog("Daily Demo Limit Reached")
def demo_limit_dialog():
    st.markdown(
        """
**You’ve reached your 10 demo leads for today.**  
RV Prospector is free to try — you’ll get 10 more leads tomorrow!  

If you’d like unlimited access, please sign up below.  
Your support keeps this project alive ❤️
        """
    )
    st.link_button("🔓 Sign Up for Extended Use", "https://your-signup-link.example")
    st.link_button("❤️ Donate via PayPal", "https://www.paypal.com/donate?hosted_button_id=YOUR_BUTTON_ID")

# --------------------------------------------------------------------------------------
# Location Helper
# --------------------------------------------------------------------------------------
US_STATES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado",
    "CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho",
    "IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
    "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
    "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
    "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"District of Columbia"
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

# --------------------------------------------------------------------------------------
# Core Search Function
# --------------------------------------------------------------------------------------
def _generate_for_user(api_key: str, email: str, location: str, requested: int, avoid_conglomerates: bool, near_me: bool, radius_m: int = 50_000) -> List[Dict[str, Any]]:
    sb = get_client()
    already = fetch_history_place_ids(sb, email)
    seen = set()
    found = []
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

    today = pd.Timestamp.today().date().isoformat()

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
                    if "locality" in types: comps["city"] = comp.get("long_name", "")
                    if "administrative_area_level_1" in types: comps["state"] = comp.get("short_name", "")
                    if "postal_code" in types: comps["zip"] = comp.get("long_name", "")

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

# --------------------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------------------
def main():
    st.markdown("<h1>🗺️ RV Prospector</h1>", unsafe_allow_html=True)
    st.caption("Find RV parks without online booking — Demo gives you 10 new leads per day.")

    sb = get_client()
    st.session_state.setdefault("log", [])

    # Login
    with st.expander("🔐 Sign In / Account", expanded=False):
        with st.form("login", border=False):
            email = st.text_input("Email (optional, for saving your history)")
            full_name = st.text_input("Full Name (optional)")
            submitted = st.form_submit_button("Sign In")

    if submitted and email and "@" in email:
        try:
            profile = upsert_profile(sb, email, full_name or None)
            unlocked = is_unlocked(sb, email)
            user_key = email
            st.success(f"✅ Signed in as {email} ({'Unlimited' if unlocked else 'Demo user'})")
        except Exception as e:
            st.warning(f"Login issue: {e}")
            user_key = _guest_key_from_cookie()
            unlocked = False
    else:
        user_key = _guest_key_from_cookie()
        unlocked = False
        st.info("Not signed in — running in demo mode (10 leads/day).")

    api_key = c.load_api_key()
    if not api_key:
        st.error("Server misconfigured: missing API key.")
        st.stop()

    st.divider()

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
        allowed, is_unlim, remaining = slice_by_trial(sb, user_key, int(requested))

        if not is_unlim and allowed <= 0:
            demo_limit_dialog()
            st.stop()

        with st.status("Searching for parks...", expanded=True) as status:
            st.session_state["log"] = []
            rows = _generate_for_user(
                api_key=api_key,
                email=user_key,
                location=location or "",
                requested=allowed,
                avoid_conglomerates=avoid_conglom,
                near_me=use_near_me,
            )
            record_history(sb, user_key, rows)
            if not is_unlim and not user_key.startswith("guest:"):
                increment_leads(sb, user_key, len(rows))
            status.update(label="✅ Done", state="complete")

        if not rows:
            st.info("No new parks found.")
            st.stop()

        # ---------------------- CLEAN DISPLAY ----------------------
        df = pd.DataFrame(rows)

        # Drop the internal ID
        if "park_place_id" in df.columns:
            df = df.drop(columns=["park_place_id"])

        # Add 1-based index
        df.insert(0, "#", range(1, len(df) + 1))

        # Make clickable park names
        if "website" in df.columns and "park_name" in df.columns:
            df["park_name"] = df.apply(
                lambda x: f"[{x['park_name']}]({x['website']})" if x["website"] else x["park_name"],
                axis=1
            )
            df = df.drop(columns=["website"])

        st.subheader(f"Results ({len(df)})")
        st.markdown(df.to_markdown(index=False), unsafe_allow_html=True)
        # ------------------------------------------------------------

        buf = io.StringIO()
        df.to_csv(buf, index=False)
        st.download_button("⬇️ Download CSV", buf.getvalue(), "rv_parks.csv", "text/csv")

        with st.expander("Run Log"):
            st.code("\n".join(st.session_state.get("log", [])))

if __name__ == "__main__":
    main()
