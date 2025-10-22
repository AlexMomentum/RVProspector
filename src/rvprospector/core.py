import os
import time
import re
import pandas as pd
from datetime import date, datetime
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter, Retry
from dotenv import dotenv_values

# ---------------- Env key management ----------------

def _user_env_dir():
    # cross-platform: %USERPROFILE% on Windows, $HOME on Unix
    home = os.path.expanduser("~")
    path = os.path.join(home, ".rvprospector")
    os.makedirs(path, exist_ok=True)
    return path

def _candidate_env_paths():
    # 1) current working dir .env
    # 2) user profile config dir ~/.rvprospector/.env
    return [os.path.join(os.getcwd(), ".env"),
            os.path.join(_user_env_dir(), ".env")]

def load_api_key():
    """
    Returns GOOGLE_PLACES_API_KEY if present in:
    1) current working dir .env, or
    2) ~/.rvprospector/.env
    or the environment.
    """
    # environment var wins
    env_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if env_key:
        return env_key

    for p in _candidate_env_paths():
        if os.path.exists(p):
            vals = dotenv_values(p)
            key = (vals.get("GOOGLE_PLACES_API_KEY") or "").strip()
            if key:
                return key
    return ""

def save_api_key(key: str, prefer="user"):
    """
    Persist the API key to an .env:
    prefer="user" -> ~/.rvprospector/.env (default)
    prefer="cwd"  -> ./ .env
    """
    key = (key or "").strip()
    if not key:
        return

    if prefer == "cwd":
        env_path = _candidate_env_paths()[0]
    else:
        env_path = _candidate_env_paths()[1]

    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    out = []
    replaced = False
    for line in lines:
        if line.strip().startswith("GOOGLE_PLACES_API_KEY="):
            out.append(f"GOOGLE_PLACES_API_KEY={key}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"GOOGLE_PLACES_API_KEY={key}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(out)

# ---------------- Core config ----------------

TARGET_QUERIES = ["RV park", "RV campground", "RV resort", "campground park"]
DEFAULT_DAILY_TARGET = 10
MAX_RESULTS_TO_CHECK = 120
PAD_MIN = 40

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
GOOGLE_TIMEOUT = 15
SUBPAGE_LIMIT = 6
TOTAL_SITE_FETCH_TIMEOUT = 18.0

DAILY_CSV = "rv_parks_daily_list.csv"
HISTORY_CSV = "rv_parks_history.csv"
DAILY_XLSX = "rv_parks_daily_list.xlsx"

BOOKING_KEYWORDS = [
    "campspot", "resnexus", "rezhub", "rezstream", "rmscloud", "rms bookings",
    "camplife", "book.now", "book-now", "bookonline", "reserveamerica",
    "koa.com", "booking.com", "siteminder", "fareharbor", "checkfront",
    "reserva", "book a site", "reserve a site", "book your stay", "reserve now"
]
BOOKING_RE = re.compile("|".join(re.escape(k) for k in BOOKING_KEYWORDS), re.IGNORECASE)

PAD_PATTERNS = [
    r"(\d{2,4})\s*(rv\s*)?(sites|pads|spaces|camp\s*sites|camp-sites|camp spaces)",
    r"(over|more than|up to)\s*(\d{2,4})\s*(rv\s*)?(sites|pads|spaces)"
]
PAD_RES = [re.compile(p, re.IGNORECASE) for p in PAD_PATTERNS]

COMMON_COLS = [
    "park_place_id",
    "date_generated","park_name","phone","website","address","city","state","zip",
    "owner_name","owner_phone","owner_email","source","booking_detected",
    "detected_keyword","pad_count","notes","call_status","outcome","follow_up_date"
]


# --- Conglomerate filtering ---
CONGLOMERATE_KEYWORDS = [
    # brands/groups to skip (name or website contains)
    "koa.com", "kampgrounds of america", "koa ",
    "thousandtrails", "thousand trails", "encore rv", "encore rv resorts",
    "sunoutdoors", "sun outdoors", "equity lifestyle", "equity lifestyles",
    "els rv", "rvonthego.com", "rvc outdoors", "bluewater", "yogi bear",
    "jellystone", "yogi bear’s jellystone", "disney fort wilderness"
]

def _is_conglomerate(name: str, website: str) -> bool:
    s = f"{(name or '').lower()} {(website or '').lower()}"
    return any(k in s for k in CONGLOMERATE_KEYWORDS)

# --- Approx "near me" via IP (best-effort) ---
def get_approx_location_via_ip(timeout=5.0):
    """
    Returns (lat, lng) floats or None if not available.
    Uses ipapi.co (no key); fallback to ipinfo.io if needed.
    """
    try:
        r = requests.get("https://ipapi.co/json", timeout=timeout)
        if r.ok:
            j = r.json()
            lat, lon = float(j.get("latitude")), float(j.get("longitude"))
            if lat and lon:
                return (lat, lon)
    except Exception:
        pass
    try:
        r = requests.get("https://ipinfo.io/json", timeout=timeout)
        if r.ok:
            j = r.json()
            loc = j.get("loc", "")
            if "," in loc:
                lat, lon = loc.split(",", 1)
                return (float(lat), float(lon))
    except Exception:
        pass
    return None

def make_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

session = make_session()

def ensure_csv(path, columns):
    if not os.path.exists(path):
        pd.DataFrame(columns=columns).to_csv(path, index=False)

ensure_csv(DAILY_CSV, COMMON_COLS)
ensure_csv(HISTORY_CSV, [
    "park_place_id","park_name","website","phone","address","city","state","zip",
    "first_seen","last_suggested_on","times_suggested","ever_called","ever_contacted","pad_count_last_known"
])

history_df = pd.read_csv(HISTORY_CSV, dtype=str)

# ---------------- Helpers ----------------

def _sanitize_url(u: str) -> str:
    if u is None:
        return ""
    s = str(u).strip()
    if s == "" or s.lower() == "nan":
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return ""

def google_text_search(api_key, query, location_bias=None, pagetoken=None, latlng=None, radius_m=50000):
    """
    If latlng=(lat,lng) is provided, uses 'location' + 'radius' for better 'near me' results.
    Otherwise falls back to 'query near {location_bias}'.
    """
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"key": api_key}
    if pagetoken:
        params["pagetoken"] = pagetoken
    else:
        params["query"] = query
        if latlng:
            lat, lng = latlng
            params["location"] = f"{lat},{lng}"
            params["radius"] = str(radius_m)   # ~50km default; adjust if you want tighter/wider
        elif location_bias:
            params["query"] = f"{query} near {location_bias}"
    resp = session.get(url, params=params, timeout=(CONNECT_TIMEOUT, GOOGLE_TIMEOUT))
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status not in ("OK", "ZERO_RESULTS"):
        raise SystemExit(f"Google Text Search error: {status} — {data.get('error_message')}")
    return data


def google_place_details(api_key, place_id):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    fields = (
        "name,formatted_address,website,formatted_phone_number,"
        "address_components,international_phone_number"
    )
    resp = session.get(url, params={"place_id": place_id, "fields": fields, "key": api_key},
                       timeout=(CONNECT_TIMEOUT, GOOGLE_TIMEOUT))
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status != "OK":
        print(f"[warn] Place details error for {place_id}: {status}")
    return data.get("result", {})

def discover_candidate_pages(base_url):
    candidates = ["", "rates", "amenities", "map", "campground-map",
                  "site-map", "camping", "rv", "rv-sites", "rv-camping", "stay", "about"]
    base = base_url.rstrip("/") + "/"
    urls = [base if slug == "" else urljoin(base, slug) for slug in candidates]
    uniq = []
    for u in urls:
        if u not in uniq:
            uniq.append(u)
        if len(uniq) >= SUBPAGE_LIMIT:
            break
    return uniq

def extract_pad_count(html):
    if not html:
        return None
    for rx in PAD_RES:
        for m in rx.finditer(html):
            nums = [int(x) for x in m.groups() if x and x.isdigit()]
            if not nums:
                continue
            n = max(nums)
            if 25 <= n <= 2000:
                return n
    return None

def check_booking_and_pads(website):
    if not website:
        return (True, "", None)
    start = time.time()
    pad_found = None
    booking_hit = ""
    for url in discover_candidate_pages(website):
        if time.time() - start > TOTAL_SITE_FETCH_TIMEOUT:
            break
        try:
            r = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if r.status_code >= 400 or not r.text:
                continue
            html = r.text
            if not booking_hit:
                m = BOOKING_RE.search(html)
                if m:
                    booking_hit = m.group(0)
            pc = extract_pad_count(html)
            if pc and (pad_found is None or pc > pad_found):
                pad_found = pc
            if booking_hit and pad_found:
                break
        except requests.RequestException:
            continue
    return (booking_hit == "", booking_hit, pad_found)

def already_seen(place_id):
    return place_id in (history_df["park_place_id"].fillna("").tolist())

def append_history_entry(entry):
    global history_df
    pid = entry["park_place_id"]
    if pid in history_df["park_place_id"].fillna("").tolist():
        idx = history_df.index[history_df["park_place_id"] == pid][0]
        history_df.at[idx, "last_suggested_on"] = entry.get("last_suggested_on", "")
        prev = history_df.at[idx, "times_suggested"] or "0"
        history_df.at[idx, "times_suggested"] = str(int(prev) + 1)
        if entry.get("pad_count_last_known"):
            history_df.at[idx, "pad_count_last_known"] = str(entry["pad_count_last_known"])
    else:
        history_df = pd.concat([history_df, pd.DataFrame([entry])], ignore_index=True)

def read_existing_authoritative():
    df = pd.DataFrame(columns=COMMON_COLS)
    if os.path.exists(DAILY_XLSX):
        try:
            df = pd.read_excel(DAILY_XLSX, dtype=str).fillna("")
        except Exception:
            pass
    if df.empty and os.path.exists(DAILY_CSV):
        try:
            df = pd.read_csv(DAILY_CSV, dtype=str).fillna("")
        except Exception:
            pass
    for c in COMMON_COLS:
        if c not in df.columns:
            df[c] = ""
    if "website" in df.columns:
        df["website"] = df["website"].apply(_sanitize_url)
    df = df[COMMON_COLS].astype(str).fillna("")
    return df

def merge_preserving_notes(existing_df: pd.DataFrame, new_rows: list) -> pd.DataFrame:
    new_df = pd.DataFrame(new_rows, columns=COMMON_COLS).fillna("")
    if new_df.empty:
        return existing_df
    ex = existing_df.set_index("park_place_id", drop=False)
    nw = new_df.set_index("park_place_id", drop=False)
    user_cols = ["notes", "call_status", "outcome", "follow_up_date",
                 "owner_name", "owner_phone", "owner_email"]
    combined = ex.combine_first(nw)
    for col in combined.columns:
        if col not in user_cols:
            combined[col] = combined[col].mask(combined[col].eq(""), nw[col])
    combined = combined[COMMON_COLS].astype(str).fillna("")
    return combined.reset_index(drop=True)

def _write_xlsx(df, target_path, website_col="website"):
    with pd.ExcelWriter(target_path, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Daily List")
        ws = writer.sheets["Daily List"]
        if website_col in df.columns:
            col_idx = df.columns.get_loc(website_col)
            for r, url in enumerate(df[website_col], start=1):
                if url:
                    ws.write_url(r, col_idx, url, string=url)

def safe_write_xlsx(df, path, website_col="website", max_retries=5):
    df = df.copy()
    if website_col in df.columns:
        df[website_col] = df[website_col].apply(_sanitize_url)
    for attempt in range(1, max_retries + 1):
        try:
            _write_xlsx(df, path, website_col)
            return path
        except PermissionError:
            if attempt == max_retries:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                alt = f"{os.path.splitext(path)[0]}_{ts}.xlsx"
                _write_xlsx(df, alt, website_col)
                print(f"[warn] {path} locked (Excel/OneDrive?). Wrote fallback: {alt}")
                return alt
            time.sleep(1.2)

def write_outputs_preserving(existing_df: pd.DataFrame, daily_rows: list):
    combined = merge_preserving_notes(existing_df, daily_rows)
    combined.to_csv(DAILY_CSV, index=False)
    safe_write_xlsx(combined, DAILY_XLSX)
    history_df.to_csv(HISTORY_CSV, index=False)
    return combined

# ---------------- Main engine ----------------

# ---------------- Main engine ----------------

def generate_daily(api_key: str,
                   location_bias: str,
                   daily_target: int,
                   avoid_conglomerates: bool = True,
                   near_me: bool = True,
                   radius_m: int = 50000,
                   progress_fn=None):   # <— NEW (optional)
    # simple event emitter
    def emit(msg: str):
        try:
            if progress_fn:
                progress_fn(msg)
        except Exception:
            pass
        print(msg)

    today = date.today().isoformat()
    found = []
    checked = 0

    # Decide search mode
    latlng = None
    if near_me:
        latlng = get_approx_location_via_ip()
        if not latlng:
            emit("[warn] Could not auto-detect location from IP; falling back to typed location.")
            near_me = False

    for query in TARGET_QUERIES:
        where = "your current area" if near_me else location_bias
        emit(f"[info] Searching: '{query}' near {where}")

        token = None
        page_num = 0
        while True:
            data = google_text_search(
                api_key,
                query,
                location_bias=None if near_me else location_bias,
                pagetoken=token,
                latlng=latlng if near_me else None,
                radius_m=radius_m
            )

            results = data.get("results", [])
            token = data.get("next_page_token")
            page_num += 1
            emit(f"  [info] Page {page_num}: {len(results)} candidates")
            if token:
                time.sleep(2.0)

            for r in results:
                if checked >= MAX_RESULTS_TO_CHECK or len(found) >= daily_target:
                    break
                pid = r.get("place_id")
                if not pid or already_seen(pid):
                    continue
                checked += 1
                name_preview = r.get("name", "")
                emit(f"    [check {checked}/{MAX_RESULTS_TO_CHECK}] {name_preview}")

                det = google_place_details(api_key, pid)
                name = det.get("name", name_preview)
                website = _sanitize_url(det.get("website", ""))
                phone = det.get("formatted_phone_number", "") or det.get("international_phone_number", "") or ""
                addr = det.get("formatted_address", "") or ""
                comps = {"city":"", "state":"", "zip":""}
                for comp in det.get("address_components", []) or []:
                    types = comp.get("types", [])
                    if "locality" in types:  comps["city"] = comp.get("long_name","")
                    if "administrative_area_level_1" in types:  comps["state"] = comp.get("short_name","")
                    if "postal_code" in types:  comps["zip"] = comp.get("long_name","")

                # (optional) conglomerate skip here if you added it
                # if avoid_conglomerates and _is_conglomerate(name, website): ... continue

                no_booking, booking_hit, pad_count = check_booking_and_pads(website)
                qualifies = no_booking and (pad_count is None or pad_count >= PAD_MIN)

                append_history_entry({
                    "park_place_id": pid, "park_name": name, "website": website, "phone": phone,
                    "address": addr, "city": comps["city"], "state": comps["state"], "zip": comps["zip"],
                    "first_seen": today, "last_suggested_on": today if qualifies else "",
                    "times_suggested": "1" if qualifies else "0", "ever_called": "",
                    "ever_contacted": "", "pad_count_last_known": str(pad_count) if pad_count is not None else ""
                })

                if qualifies:
                    found.append({
                        "park_place_id": pid, "date_generated": today, "park_name": name, "phone": phone,
                        "website": website, "address": addr, "city": comps["city"], "state": comps["state"],
                        "zip": comps["zip"], "owner_name": "", "owner_phone": "", "owner_email": "",
                        "source": "Google Places", "booking_detected": False if not booking_hit else True,
                        "detected_keyword": booking_hit, "pad_count": pad_count if pad_count is not None else "",
                        "notes": "Pad count inferred from site" if pad_count else "Verify pad count by phone",
                        "call_status": "", "outcome": "", "follow_up_date": ""
                    })
                    emit(f"      [keep] {name} (pads: {pad_count if pad_count else 'unknown'}, no booking: {not booking_hit})")

            if not token or checked >= MAX_RESULTS_TO_CHECK or len(found) >= daily_target:
                break
        if len(found) >= daily_target or checked >= MAX_RESULTS_TO_CHECK:
            break

    existing = read_existing_authoritative()
    write_outputs_preserving(existing, found)
    emit(f"[done] Suggested today: {len(found)} / {daily_target} (>= {PAD_MIN} pads, no booking). Checked: {checked}")
    if len(found) < daily_target:
        emit("[tip] Increase RV_MAX_CHECKS, broaden location, or add more queries.")
        emit("[note] Pad counts are inferred heuristically; confirm on the call.")

