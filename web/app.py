# web/app.py
"""
RV Prospector - Enhanced with radial search pattern
"""
from __future__ import annotations

import os
import io
import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# Local imports
from db import (
    get_client,
    upsert_profile,
    is_unlocked,
    get_leads_used_today,
    slice_by_trial,
    fetch_history_place_ids,
    list_history_rows,
    list_history_all,
    record_history,
    DEMO_LIMIT,
)
from lead_sync import subscribe_mailchimp
from places_search import RVParkFinder, format_place_for_db

# Load environment
WEB_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEB_DIR.parent
HOME = Path.home()

for p in (WEB_DIR / ".env", ROOT_DIR / ".env", HOME / ".rvprospector" / ".env"):
    if Path(p).exists():
        load_dotenv(dotenv_path=p, override=True)

# Page config
st.set_page_config(
    page_title="RV Prospector - Find RV Parks Without Online Booking",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        text-align: center;
        margin-bottom: 1rem;
    }
    .sub-header {
        font-size: 1.2rem;
        color: #666;
        text-align: center;
        margin-bottom: 2rem;
    }
    .info-box {
        background-color: #f0f8ff;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #1f77b4;
        margin: 1rem 0;
    }
    .warning-box {
        background-color: #fff3cd;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #ffc107;
        margin: 1rem 0;
    }
    .success-box {
        background-color: #d4edda;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 4px solid #28a745;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

# Initialize session state
if 'email' not in st.session_state:
    st.session_state.email = ""
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if 'search_results' not in st.session_state:
    st.session_state.search_results = []
if 'history_data' not in st.session_state:
    st.session_state.history_data = []


def authenticate_user(email: str, full_name: Optional[str] = None) -> bool:
    """Authenticate user and create/update profile."""
    try:
        sb = get_client()
        upsert_profile(sb, email, full_name)
        st.session_state.email = email
        st.session_state.authenticated = True
        
        # Subscribe to Mailchimp (non-blocking)
        try:
            subscribe_mailchimp(email)
        except Exception:
            pass  # Don't fail on Mailchimp errors
        
        return True
    except Exception as e:
        st.error(f"Authentication error: {e}")
        return False


def get_user_location() -> tuple[float, float] | None:
    """Get user's location via browser geolocation or manual input."""
    st.sidebar.subheader("📍 Your Location")
    
    location_method = st.sidebar.radio(
        "How to set location:",
        ["Use Current Location", "Enter Manually"],
        key="location_method"
    )
    
    if location_method == "Enter Manually":
        col1, col2 = st.sidebar.columns(2)
        with col1:
            lat = st.number_input("Latitude", value=33.4484, format="%.4f", key="manual_lat")
        with col2:
            lng = st.number_input("Longitude", value=-112.0740, format="%.4f", key="manual_lng")
        
        if st.sidebar.button("Use This Location", key="use_manual"):
            return (lat, lng)
    else:
        st.sidebar.info("🔄 Click below to get your current location from your browser")
        
        # JavaScript to get geolocation
        location_js = """
        <script>
        function getLocation() {
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(
                    function(position) {
                        // Send location to Streamlit
                        const lat = position.coords.latitude;
                        const lng = position.coords.longitude;
                        
                        // Store in localStorage for Streamlit to read
                        localStorage.setItem('user_lat', lat);
                        localStorage.setItem('user_lng', lng);
                        
                        alert('Location captured: ' + lat + ', ' + lng);
                    },
                    function(error) {
                        alert('Error getting location: ' + error.message);
                    }
                );
            } else {
                alert('Geolocation not supported by this browser');
            }
        }
        </script>
        <button onclick="getLocation()" style="padding: 10px 20px; background: #1f77b4; color: white; border: none; border-radius: 5px; cursor: pointer;">
            Get My Location
        </button>
        """
        
        st.sidebar.markdown(location_js, unsafe_allow_html=True)
        
        # Fallback: manual input after trying geolocation
        st.sidebar.markdown("---")
        st.sidebar.caption("Or enter manually if geolocation doesn't work:")
        col1, col2 = st.sidebar.columns(2)
        with col1:
            lat = st.number_input("Latitude", value=33.4484, format="%.4f", key="fallback_lat")
        with col2:
            lng = st.number_input("Longitude", value=-112.0740, format="%.4f", key="fallback_lng")
        
        if st.sidebar.button("Use These Coordinates", key="use_fallback"):
            return (lat, lng)
    
    return None


def search_rv_parks(
    origin_lat: float,
    origin_lng: float,
    max_radius_km: int,
    max_results: int,
    email: str
) -> list[dict]:
    """
    Perform radial search for RV parks without online booking.
    """
    try:
        sb = get_client()
        
        # Check limits
        allowed, unlocked, remaining = slice_by_trial(sb, email, max_results)
        
        if allowed == 0:
            st.warning(f"⚠️ You've reached your daily limit of {DEMO_LIMIT} leads. Upgrade for unlimited access!")
            return []
        
        # Show user what they can get
        if not unlocked and allowed < max_results:
            st.info(f"ℹ️ Demo users can find up to {DEMO_LIMIT} leads per day. You have {remaining} searches remaining today.")
        
        # Get API key
        api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        if not api_key:
            st.error("❌ Google Places API key not configured. Please add GOOGLE_PLACES_API_KEY to your .env file.")
            return []
        
        # Initialize finder
        finder = RVParkFinder(api_key)
        
        # Show progress
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        status_text.text("🔍 Searching for RV parks in all directions...")
        
        # Perform search
        keywords = ["RV park", "RV resort", "mobile home park", "trailer park"]
        
        results = finder.find_parks(
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            max_radius_km=max_radius_km,
            step_km=40,  # Search every 40km
            exclude_online_booking=True,  # Only parks WITHOUT online booking
            keywords=keywords
        )
        
        progress_bar.progress(0.7)
        status_text.text("📊 Formatting results...")
        
        # Filter out duplicates from history
        history_ids = fetch_history_place_ids(sb, email)
        new_results = [r for r in results if r.get('place_id') not in history_ids]
        
        # Limit to allowed count
        new_results = new_results[:allowed]
        
        progress_bar.progress(0.9)
        status_text.text("💾 Saving to your account...")
        
        # Format for database
        formatted_results = [
            format_place_for_db(place, place.get('detected_keyword', ''))
            for place in new_results
        ]
        
        # Save to history
        if formatted_results:
            record_history(sb, email, formatted_results)
        
        progress_bar.progress(1.0)
        status_text.text("✅ Search complete!")
        
        return formatted_results
        
    except Exception as e:
        st.error(f"❌ Search error: {e}")
        return []


def display_results(results: list[dict]):
    """Display search results in a nice format."""
    if not results:
        st.info("No results found. Try adjusting your search parameters.")
        return
    
    st.success(f"✅ Found {len(results)} RV parks without online booking!")
    
    # Convert to DataFrame for display
    df = pd.DataFrame(results)
    
    # Reorder columns for better display
    display_cols = ['park_name', 'phone', 'website', 'city', 'state', 'detected_keyword']
    available_cols = [col for col in display_cols if col in df.columns]
    df_display = df[available_cols]
    
    # Show interactive table
    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "park_name": st.column_config.TextColumn("Park Name", width="medium"),
            "phone": st.column_config.TextColumn("Phone", width="small"),
            "website": st.column_config.LinkColumn("Website", width="medium"),
            "city": st.column_config.TextColumn("City", width="small"),
            "state": st.column_config.TextColumn("State", width="small"),
            "detected_keyword": st.column_config.TextColumn("Type", width="small"),
        }
    )
    
    # Download button
    csv = df.to_csv(index=False)
    st.download_button(
        label="📥 Download as CSV",
        data=csv,
        file_name=f"rv_parks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True
    )


def show_history_page():
    """Display user's search history."""
    st.markdown('<div class="main-header">📚 Search History</div>', unsafe_allow_html=True)
    
    try:
        sb = get_client()
        rows = list_history_rows(sb, st.session_state.email, limit=1000)
        
        if not rows:
            st.info("No search history yet. Perform a search to get started!")
            return
        
        st.success(f"You have {len(rows)} saved leads")
        
        # Convert to DataFrame
        df = pd.DataFrame(rows)
        
        # Add date formatting
        if 'created_at' in df.columns:
            df['created_at'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d %H:%M')
        
        # Display
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )
        
        # Download all history
        all_rows = list_history_all(sb, st.session_state.email)
        if all_rows:
            df_all = pd.DataFrame(all_rows)
            csv_all = df_all.to_csv(index=False)
            
            st.download_button(
                label="📥 Download All History as CSV",
                data=csv_all,
                file_name=f"rv_parks_history_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True
            )
    
    except Exception as e:
        st.error(f"Error loading history: {e}")


def show_main_page():
    """Main search interface."""
    st.markdown('<div class="main-header">🚐 RV Prospector</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Find RV Parks & Mobile Home Communities Without Online Booking</div>',
        unsafe_allow_html=True
    )
    
    # Check account status
    try:
        sb = get_client()
        unlocked = is_unlocked(sb, st.session_state.email)
        used_today = get_leads_used_today(sb, st.session_state.email)
        remaining = max(0, DEMO_LIMIT - used_today)
        
        if unlocked:
            st.markdown(
                '<div class="success-box">✨ <strong>Unlimited Account</strong> - Search as much as you want!</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                f'<div class="info-box">📊 Demo Account - {remaining} of {DEMO_LIMIT} daily searches remaining</div>',
                unsafe_allow_html=True
            )
    except Exception:
        pass
    
    # Get location
    location = get_user_location()
    
    # Search parameters
    st.subheader("🔍 Search Parameters")
    
    col1, col2 = st.columns(2)
    
    with col1:
        max_radius = st.slider(
            "Search Radius (miles)",
            min_value=25,
            max_value=200,
            value=100,
            step=25,
            help="How far to search in all directions from your location"
        )
        max_radius_km = int(max_radius * 1.60934)  # Convert to km
    
    with col2:
        max_results = st.number_input(
            "Maximum Results",
            min_value=10,
            max_value=500,
            value=50,
            step=10,
            help="Maximum number of leads to find"
        )
    
    # Search button
    if st.button("🔍 Find RV Parks", type="primary", use_container_width=True):
        if not location:
            st.warning("⚠️ Please set your location first using the sidebar")
        else:
            lat, lng = location
            st.info(f"📍 Searching from location: {lat:.4f}, {lng:.4f}")
            
            results = search_rv_parks(
                origin_lat=lat,
                origin_lng=lng,
                max_radius_km=max_radius_km,
                max_results=int(max_results),
                email=st.session_state.email
            )
            
            st.session_state.search_results = results
    
    # Display results
    if st.session_state.search_results:
        st.markdown("---")
        display_results(st.session_state.search_results)


def main():
    """Main app entry point."""
    
    # Sidebar for authentication
    with st.sidebar:
        st.title("🚐 RV Prospector")
        
        if not st.session_state.authenticated:
            st.subheader("Sign In / Sign Up")
            
            email = st.text_input("Email Address", key="login_email")
            full_name = st.text_input("Full Name (optional)", key="login_name")
            
            if st.button("Continue", use_container_width=True):
                if email:
                    if authenticate_user(email, full_name):
                        st.success("✅ Signed in successfully!")
                        st.rerun()
                else:
                    st.error("Please enter your email")
        else:
            st.success(f"✅ Signed in as: {st.session_state.email}")
            if st.button("Sign Out", use_container_width=True):
                st.session_state.authenticated = False
                st.session_state.email = ""
                st.rerun()
            
            st.markdown("---")
    
    # Main content
    if not st.session_state.authenticated:
        st.markdown('<div class="main-header">🚐 Welcome to RV Prospector</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="info-box">'
            '<h3>Find RV Parks Without Online Booking</h3>'
            '<p>Our advanced search system scans in all directions from your location to find:</p>'
            '<ul>'
            '<li>✅ RV Parks without online booking systems</li>'
            '<li>✅ Mobile Home Communities</li>'
            '<li>✅ Contact information (phone, website)</li>'
            '<li>✅ Full addresses and details</li>'
            '</ul>'
            '<p><strong>Please sign in using the sidebar to get started.</strong></p>'
            '</div>',
            unsafe_allow_html=True
        )
    else:
        # Navigation
        page = st.sidebar.radio(
            "Navigation",
            ["🔍 Search", "📚 History"],
            key="nav"
        )
        
        if page == "🔍 Search":
            show_main_page()
        else:
            show_history_page()


if __name__ == "__main__":
    main()
