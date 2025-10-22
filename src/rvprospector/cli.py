import os
import argparse
from .core import generate_daily, load_api_key, DEFAULT_DAILY_TARGET
from .ui import run_ui_default

def main():
    p = argparse.ArgumentParser(description="RV Prospector")
    p.add_argument("--no-ui", action="store_true", help="Run headless (skip Tk UI).")
    p.add_argument("--location", type=str, help="Location bias (e.g., 'Charlotte, NC').")
    p.add_argument("--target", type=int, help="New prospects to add today (default 10).")
    p.add_argument("--api-key", type=str, help="Google Places API key (overrides .env).")
    args = p.parse_args()

    # UI by default:
    if not args.no_ui:
        run_ui_default()
        return

    # Headless:
    api = args.api_key or load_api_key()
    if not api:
        raise SystemExit("ERROR: GOOGLE_PLACES_API_KEY not set. Use --api-key or create .env (cwd or ~/.rvprospector/.env).")

    loc = args.location or os.getenv("RV_LOCATION", "Charlotte, NC")
    tgt = args.target if args.target is not None else int(os.getenv("RV_DAILY_TARGET", str(DEFAULT_DAILY_TARGET)))
    tgt = max(1, min(200, int(tgt)))

    generate_daily(api, loc, tgt)
