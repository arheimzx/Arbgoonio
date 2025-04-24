import os
import time
import threading
import json
import requests
import logging
from collections import deque
from flask import Flask, render_template_string, request, jsonify

# ────────────────────────── CONFIGURATION ──────────────────────────
API_BASE = "https://gamma-api.polymarket.com"
SCAN_INTERVAL = 10  # seconds between API polls
REFRESH_INTERVAL = 5  # seconds between browser refreshes
HISTORY_MINUTES = 5  # minutes to keep price movement history
MAX_MOVES = 500  # maximum number of moves to store (memory optimization)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ────────────────────────── APP & STATE ──────────────────────────
app = Flask(__name__)

# Global shared state - protected by lock
lock = threading.RLock()
events_meta = {}  # id -> {id,title,link}
events_data = {}  # id -> {id,title,link,markets:[...]}
last_prices = {}  # market_id -> (yes,no)
recent_moves = deque(maxlen=MAX_MOVES)


# ────────────────────────── TEMPLATES ───────────────────────────
# [template definitions skipped for brevity]

# ────────────────────────── DEBUGGING ROUTES ────────────────────
@app.route('/debug-info')
def debug_info():
    """Endpoint to check what data exists in the application state"""
    with lock:
        return jsonify({
            'events_count': len(events_data),
            'events_meta_count': len(events_meta),
            'last_prices_count': len(last_prices),
            'recent_moves_count': len(recent_moves),
            'events_sample': list(events_data.keys())[:5] if events_data else []
        })


@app.route('/debug-reset')
def debug_reset():
    """Force a fresh data fetch - useful for debugging"""
    global events_meta, events_data, last_prices, recent_moves

    with lock:
        events_meta.clear()
        events_data.clear()
        last_prices.clear()
        recent_moves.clear()

    # Force an immediate scan
    threading.Thread(target=fetch_and_process_data, daemon=True).start()

    return jsonify({'status': 'reset_initiated'})


# ────────────────────────── API HELPERS ─────────────────────────
def fetch_all_events(params=None, page_size=50, max_retries=3):
    """Fetch all events with pagination - smaller page size for reliability"""
    params = params or {}
    results = []
    offset = 0

    logger.info(f"Starting API fetch with params: {params}")

    try:
        while True:
            q = params.copy()
            q.update(limit=page_size, offset=offset)

            success = False
            for attempt in range(max_retries):
                try:
                    logger.debug(f"Fetching page at offset {offset}")
                    response = requests.get(f"{API_BASE}/events", params=q, timeout=15)
                    response.raise_for_status()
                    batch = response.json()

                    if not batch:
                        logger.debug("Received empty batch")
                        break

                    logger.debug(f"Received batch with {len(batch)} events")
                    results.extend(batch)

                    if len(batch) < page_size:
                        break

                    offset += page_size
                    success = True
                    break

                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"API fetch failed after {max_retries} attempts: {e}")
                    else:
                        logger.warning(f"API fetch attempt {attempt + 1} failed: {e}")
                        time.sleep(1)

            if not success or len(batch) < page_size:
                break

        logger.info(f"Total events fetched: {len(results)}")
        return results

    except Exception as e:
        logger.error(f"Unexpected error in fetch_all_events: {e}", exc_info=True)
        return []


def make_event_url(event):
    """Create URL for an event"""
    return f"https://polymarket.com/event/{event.get('slug', event['id'])}?tid={event['id']}"


def parse_prices(raw):
    """Parse price data safely"""
    if not raw:
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None

    try:
        return float(raw[0]), float(raw[1])
    except (IndexError, TypeError, ValueError):
        return None


# ────────────────────────── DATA PROCESSING ────────────────────────
def fetch_and_process_data():
    """Fetch and process data from API - can be called directly for debug"""
    global events_meta, events_data, last_prices, recent_moves

    logger.info("Manual data fetch initiated")
    params = {"closed": False, "archived": False, "active": True}

    try:
        # Fetch events from API
        all_events = fetch_all_events(params)
        logger.info(f"Fetched {len(all_events)} events")

        now = time.time()
        updated_events = {}
        new_moves = []

        # Process each event
        for event in all_events:
            event_id = event["id"]

            with lock:
                # Update metadata
                meta = events_meta.setdefault(
                    event_id,
                    {"id": event_id, "title": event["title"], "link": make_event_url(event)}
                )
                meta["title"] = event["title"]

                # Process markets for this event
                markets_data = []
                for market in event.get("markets", []):
                    prices = parse_prices(market.get("outcomePrices"))
                    if not prices:
                        continue

                    yes_price, no_price = prices
                    prev_yes, prev_no = last_prices.get(market["id"], (yes_price, no_price))
                    yes_change = (yes_price - prev_yes) * 100
                    no_change = (no_price - prev_no) * 100

                    market_record = {
                        "id": market["id"],
                        "question": market["question"],
                        "yes": yes_price * 100,
                        "no": no_price * 100,
                        "yd": abs(yes_change),
                        "nd": abs(no_change),
                        "ydir": "UP" if yes_change > 0 else ("DOWN" if yes_change < 0 else "—"),
                        "ndir": "UP" if no_change > 0 else ("DOWN" if no_change < 0 else "—"),
                        "max_move": max(abs(yes_change), abs(no_change))
                    }
                    markets_data.append(market_record)

                    # Record price moves if there were any
                    if yes_change != 0 or no_change != 0:
                        move = {
                            "time_ts": now,
                            "event_id": event_id,
                            "event_title": event["title"],
                            "event_link": make_event_url(event),
                            "market_id": market["id"],
                            "question": market["question"],
                            "max_move": max(abs(yes_change), abs(no_change)),
                            **market_record
                        }
                        recent_moves.append(move)
                        new_moves.append(move)

                    # Always update last prices
                    last_prices[market["id"]] = (yes_price, no_price)

                # Only store events with markets
                if markets_data:
                    updated_events[event_id] = {**meta, "markets": markets_data}

        # Update global state
        with lock:
            # Add/update events found in this scan
            for eid, event_data in updated_events.items():
                events_data[eid] = event_data

            logger.info(f"Updated state: {len(events_data)} events in events_data")
            for k in list(events_data.keys())[:5]:
                logger.debug(f"Sample event: {k} - {events_data[k]['title']}")

    except Exception as e:
        logger.error(f"Error in fetch_and_process_data: {e}", exc_info=True)


# ────────────────────────── BACKGROUND SCANNER ─────────────────────
def scan_loop():
    """Background thread that polls for data"""
    logger.info("🔁 Background scanner started")

    # Do an initial scan
    fetch_and_process_data()

    # Then scan periodically
    while True:
        try:
            time.sleep(SCAN_INTERVAL)
            fetch_and_process_data()
        except Exception as e:
            logger.error(f"Error in scan_loop: {e}")
            time.sleep(5)  # Shorter wait on error


# ────────────────────────── ROUTES ──────────────────────────────
@app.route('/')
def index():
    """Home page - display all events"""
    now = time.time()

    with lock:
        # Get current events
        events_list = list(events_data.values())
        logger.info(f"📦 Sending {len(events_list)} events to frontend")

        # Get new moves for display
        new_moves = [m for m in recent_moves if now - m["time_ts"] < SCAN_INTERVAL]

        # Debug info
        if not events_list:
            logger.warning("No events available to display!")
            logger.info(f"events_data has {len(events_data)} entries")
            logger.info(f"events_meta has {len(events_meta)} entries")

    # Template rendering skipped for brevity - use your existing template

    return f"""
    <html>
    <head><title>Polymarket Scanner</title></head>
    <body style="font-family: sans-serif; background: #222; color: white; padding: 20px;">
        <h1>Polymarket Scanner</h1>
        <p>Events available: {len(events_list)}</p>
        <p>Recent moves: {len(new_moves)}</p>

        <h2>Debug Info</h2>
        <a href="/debug-info" style="color: cyan;">View Debug Info</a> | 
        <a href="/debug-reset" style="color: cyan;">Reset Data</a>

        <h2>Events</h2>
        {'<div style="color: yellow;">No events available. Please try resetting data.</div>' if not events_list else ''}

        <ul>
            {' '.join(f'<li>{e["title"]} ({len(e.get("markets", []))} markets)</li>' for e in events_list[:10])}
            {f'<li>... and {len(events_list) - 10} more</li>' if len(events_list) > 10 else ''}
        </ul>
    </body>
    </html>
    """


# ────────────────────────── STARTUP ─────────────────────────────
# Start background scanner thread
scanner_thread = threading.Thread(target=scan_loop, daemon=True)
scanner_thread.start()

# Flask app configuration for Render.com
if __name__ == "__main__":
    app.run(debug=False)