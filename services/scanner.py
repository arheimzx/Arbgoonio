import threading
import time
import logging
from collections import deque
import json

from config import SCAN_INTERVAL, MAX_MOVES
from api.polymarket_api import fetch_all_events, make_event_url, parse_prices
from services.file_manager import (
    save_events_to_file, load_events_from_file,
    save_moves_to_file, load_moves_from_file,
    save_status, load_status
)

# Configure logging
logger = logging.getLogger(__name__)

# Global state variables
events_meta = {}  # id -> {id,title,link}
events_data = {}  # id -> {id,title,link,markets:[…]}
last_prices = {}  # market_id -> (yes,no)
recent_moves = deque(maxlen=MAX_MOVES)  # Fixed-size queue
lock = threading.RLock()  # For thread safety


def scan_loop():
    """Main scanning loop to poll the Polymarket API"""
    global events_meta, events_data, last_prices, recent_moves

    logger.info("🔁 Scan loop started")
    status = {"status": "Starting scan", "last_update": time.time()}
    save_status(status)

    params = {"closed": False, "archived": False, "active": True}

    # Load existing data from files
    try:
        file_events = load_events_from_file()
        if file_events:
            events_data = file_events
            events_meta = {eid: {"id": eid, "title": ev["title"], "link": ev["link"]}
                          for eid, ev in events_data.items()}
            logger.info(f"Loaded {len(events_data)} events from file")

        file_moves = load_moves_from_file()
        if file_moves:
            recent_moves = deque(file_moves, maxlen=MAX_MOVES)
            logger.info(f"Loaded {len(recent_moves)} moves from file")
    except Exception as e:
        logger.error(f"Error loading data from files: {e}")

    # initial seed
    try:
        status["status"] = "Initial scan"
        save_status(status)

        for ev in fetch_all_events(params):
            eid = ev["id"]
            with lock:
                events_meta[eid] = {
                    "id": eid,
                    "title": ev["title"],
                    "link": make_event_url(ev),
                    # Add event-level volume and liquidity data
                    "volume": float(ev.get("volume", 0)),
                    "volume24hr": float(ev.get("volume24hr", 0)),
                    "liquidity": float(ev.get("liquidity", 0))
                }
                for m in ev.get("markets", []):
                    p = parse_prices(m.get("outcomePrices", []))
                    if p:
                        last_prices[m["id"]] = p

        logger.info(f"Initial scan completed, found {len(events_meta)} events")
        status["status"] = f"Initial scan completed, found {len(events_meta)} events"
        status["last_update"] = time.time()
        save_status(status)
    except Exception as e:
        logger.error(f"Error during initial scan: {e}")
        status["status"] = f"Error during initial scan: {str(e)[:100]}"
        save_status(status)

    while True:
        time.sleep(SCAN_INTERVAL)
        now = time.time()
        updated = {}
        new_moves = []
        sound_trigger = None  # Will store sound info for browser playback

        try:
            status["status"] = "Scanning for updates"
            save_status(status)

            for ev in fetch_all_events(params):
                eid = ev["id"]
                with lock:
                    meta = events_meta.setdefault(
                        eid,
                        {
                            "id": eid,
                            "title": ev["title"],
                            "link": make_event_url(ev),
                            "volume": float(ev.get("volume", 0)),
                            "volume24hr": float(ev.get("volume24hr", 0)),
                            "liquidity": float(ev.get("liquidity", 0))
                        }
                    )
                    # Update event metadata
                    meta["title"] = ev["title"]
                    meta["volume"] = float(ev.get("volume", 0))
                    meta["volume24hr"] = float(ev.get("volume24hr", 0))
                    meta["liquidity"] = float(ev.get("liquidity", 0))

                    mkts = []
                    total_volume = 0
                    total_liquidity = 0

                    for m in ev.get("markets", []):
                        p = parse_prices(m.get("outcomePrices", []))
                        if not p:
                            continue

                        y, n = p
                        py, pn = last_prices.get(m["id"], (y, n))
                        dy, dn = (y - py) * 100, (n - pn) * 100

                        # Extract volume and liquidity data for the market
                        market_volume = float(m.get("volume", 0))
                        market_liquidity = float(m.get("liquidity", 0))
                        market_volume24hr = float(m.get("volume24hr", 0))

                        # Add to totals
                        total_volume += market_volume
                        total_liquidity += market_liquidity

                        rec = {
                            "id": m["id"],
                            "question": m["question"],
                            "yes": y * 100,
                            "no": n * 100,
                            "yd": abs(dy),
                            "nd": abs(dn),
                            "ydir": "UP" if dy > 0 else ("DOWN" if dy < 0 else "—"),
                            "ndir": "UP" if dn > 0 else ("DOWN" if dn < 0 else "—"),
                            "max_move": max(abs(dy), abs(dn)),
                            # Add volume and liquidity data
                            "volume": market_volume,
                            "volume24hr": market_volume24hr,
                            "liquidity": market_liquidity
                        }
                        mkts.append(rec)

                        # record move
                        if dy != 0 or dn != 0:
                            move = {
                                "time_ts": now,
                                "event_id": eid,
                                "event_title": ev["title"],
                                "event_link": make_event_url(ev),
                                "market_id": m["id"],
                                "question": m["question"],
                                "max_move": max(abs(dy), abs(dn)),
                                "volume": market_volume,
                                "liquidity": market_liquidity,
                                "event_volume": meta["volume"],  # Add event volume for terminal
                                **rec
                            }
                            recent_moves.append(move)
                            new_moves.append(move)

                        last_prices[m["id"]] = (y, n)

                    if mkts:  # Only add events with markets
                        # Add calculated totals to the event
                        updated[eid] = {
                            **meta,
                            "markets": mkts,
                            "total_volume": total_volume,
                            "total_liquidity": total_liquidity
                        }

                # Automatically clean up events with no markets
                if not mkts and eid in events_data:
                    with lock:
                        if eid in events_data:
                            del events_data[eid]
                        if eid in events_meta:
                            del events_meta[eid]

            with lock:
                # Update events_data and save to file
                if updated:
                    events_data = updated
                    save_events_to_file(events_data)

                # Save recent moves to file
                save_moves_to_file(recent_moves)

            # Determine if we should trigger a sound in the browser
            if new_moves:
                top = max(new_moves, key=lambda m: max(m["yd"], m["nd"]))
                mag = max(top["yd"], top["nd"])
                if mag > 5:
                    sound_trigger = {"level": "high", "magnitude": mag}
                elif mag > 1:
                    sound_trigger = {"level": "medium", "magnitude": mag}
                elif mag > 0.3:
                    sound_trigger = {"level": "low", "magnitude": mag}

            # Update status with sound trigger info
            status["status"] = f"Updated {len(events_data)} events with markets"
            status["last_update"] = now
            if sound_trigger:
                status["sound_trigger"] = sound_trigger
            else:
                status.pop("sound_trigger", None)  # Remove if exists
            save_status(status)

            logger.info(f"Scanned {len(events_data)} events, found {len(new_moves)} moves")

        except Exception as e:
            logger.error(f"Scan error: {e}")
            status["status"] = f"Scan error: {str(e)[:100]}"
            save_status(status)


def start_scanner():
    """Start the scanner as a background thread"""
    threading.Thread(target=scan_loop, daemon=True).start()


# Expose functions and variables
__all__ = [
    'start_scanner',
    'events_meta',
    'events_data',
    'last_prices',
    'recent_moves',
    'lock'
]