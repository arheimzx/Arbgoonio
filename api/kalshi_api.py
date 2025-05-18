import os
import sys
import logging
import threading
import time
from collections import deque
import requests

# Add the project root directory to the Python path
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from services.file_manager import (load_events_from_file, load_moves_from_file,
                                   save_events_to_file, save_moves_to_file, save_status)
from config import SCAN_INTERVAL, MAX_MOVES, KALSHI_PUBLIC_API

logger = logging.getLogger(__name__)

# Global state variables
events_meta = {}  # ticker -> {ticker, title, link}
events_data = {}  # ticker -> {ticker, title, link, markets:[…]}
last_prices = {}  # market_ticker -> (yes,no)
recent_moves = deque(maxlen=MAX_MOVES)  # Fixed-size queue
scanner_lock = threading.RLock()  # For thread safety

# File paths for Kalshi data
KALSHI_EVENTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data/kalshi_events_data.json")
KALSHI_MOVES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data/kalshi_recent_moves.json")
KALSHI_STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data/kalshi_status.json")


def fetch_all_events(active_only=True):
    """Fetch all events from Kalshi public API"""
    try:
        # Fetch events from public API
        response = requests.get(f"{KALSHI_PUBLIC_API}/events")
        response.raise_for_status()

        event_data = response.json()
        events = event_data.get('events', [])

        # Filter active events if requested
        if active_only:
            events = [e for e in events if e.get('status') == 'active']

        return events
    except Exception as e:
        logger.error(f"Error fetching Kalshi events: {e}")
        return []


def fetch_markets_for_event(event_ticker):
    """Fetch markets for a specific event"""
    try:
        response = requests.get(f"{KALSHI_PUBLIC_API}/events/{event_ticker}/markets")
        response.raise_for_status()

        market_data = response.json()
        return market_data.get('markets', [])
    except Exception as e:
        logger.error(f"Error fetching markets for event {event_ticker}: {e}")
        return []


def fetch_market_prices(market_ticker):
    """Fetch current prices for a market"""
    try:
        response = requests.get(f"{KALSHI_PUBLIC_API}/markets/{market_ticker}")
        response.raise_for_status()

        market_data = response.json()
        market = market_data.get('market', {})

        # Get the yes price from the market data
        yes_price = market.get('yes_bid', 0)
        if yes_price is None:
            yes_price = 0

        # Calculate no price (1 - yes_price)
        no_price = 1 - yes_price

        return (yes_price, no_price)
    except Exception as e:
        logger.error(f"Error fetching prices for market {market_ticker}: {e}")
        return None


def save_kalshi_events_to_file(events_data):
    """Save Kalshi events data to file"""
    try:
        with open(KALSHI_EVENTS_FILE, 'w') as f:
            import json
            json.dump(events_data, f)
        logger.info(f"Saved {len(events_data)} Kalshi events to file")
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi events to file: {e}")
        return False


def load_kalshi_events_from_file():
    """Load Kalshi events data from file"""
    try:
        if os.path.exists(KALSHI_EVENTS_FILE):
            with open(KALSHI_EVENTS_FILE, 'r') as f:
                import json
                data = json.load(f)
            logger.info(f"Loaded {len(data)} Kalshi events from file")
            return data
        return {}
    except Exception as e:
        logger.error(f"Error loading Kalshi events from file: {e}")
        return {}


def save_kalshi_moves_to_file(moves):
    """Save Kalshi recent moves to file"""
    try:
        moves_list = list(moves)
        with open(KALSHI_MOVES_FILE, 'w') as f:
            import json
            json.dump(moves_list, f)
        logger.info(f"Saved {len(moves_list)} Kalshi moves to file")
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi moves to file: {e}")
        return False


def load_kalshi_moves_from_file():
    """Load Kalshi recent moves from file"""
    try:
        if os.path.exists(KALSHI_MOVES_FILE):
            with open(KALSHI_MOVES_FILE, 'r') as f:
                import json
                data = json.load(f)
            logger.info(f"Loaded {len(data)} Kalshi moves from file")
            return deque(data, maxlen=MAX_MOVES)
        return deque(maxlen=MAX_MOVES)
    except Exception as e:
        logger.error(f"Error loading Kalshi moves from file: {e}")
        return deque(maxlen=MAX_MOVES)


def save_kalshi_status(status_data):
    """Save Kalshi status to file"""
    try:
        with open(KALSHI_STATUS_FILE, 'w') as f:
            import json
            json.dump(status_data, f)
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi status to file: {e}")
        return False


def load_kalshi_status():
    """Load Kalshi status from file"""
    try:
        if os.path.exists(KALSHI_STATUS_FILE):
            with open(KALSHI_STATUS_FILE, 'r') as f:
                import json
                return json.load(f)
        return {"last_update": 0, "status": "Not started"}
    except Exception as e:
        logger.error(f"Error loading Kalshi status from file: {e}")
        return {"last_update": 0, "status": "Error loading status"}


class KalshiScannerService:
    """Service to handle background scanning of Kalshi data"""

    @staticmethod
    def scan_loop():
        """Main scanning loop to poll the Kalshi API"""
        global events_meta, events_data, last_prices, recent_moves

        logger.info("🔁 Kalshi Scan loop started")
        status = {"status": "Starting scan", "last_update": time.time()}
        save_kalshi_status(status)

        # Load existing data from files
        try:
            file_events = load_kalshi_events_from_file()
            if file_events:
                events_data = file_events
                events_meta = {
                    ticker: {"ticker": ticker, "title": ev["title"], "link": f"https://kalshi.com/markets/{ticker}"}
                    for ticker, ev in events_data.items()}
                logger.info(f"Loaded {len(events_data)} Kalshi events from file")

            file_moves = load_kalshi_moves_from_file()
            if file_moves:
                recent_moves = file_moves
                logger.info(f"Loaded {len(recent_moves)} Kalshi moves from file")
        except Exception as e:
            logger.error(f"Error loading Kalshi data from files: {e}")

        # Initial seed
        try:
            status["status"] = "Initial scan"
            save_kalshi_status(status)

            for ev in fetch_all_events():
                ticker = ev.get('ticker')
                with scanner_lock:
                    events_meta[ticker] = {
                        "ticker": ticker,
                        "title": ev.get('series_name', '') + ': ' + ev.get('title', ''),
                        "link": f"https://kalshi.com/events/{ticker}",
                        # Add event-level data
                        "volume": float(ev.get('volume', 0)),
                        "liquidity": float(ev.get('liquidity', 0))
                    }

                    # Process markets for the event
                    markets = []
                    event_markets = fetch_markets_for_event(ticker)

                    for m in event_markets:
                        market_ticker = m.get('ticker')
                        prices = fetch_market_prices(market_ticker)

                        if prices:
                            last_prices[market_ticker] = prices

                            market_data = {
                                "ticker": market_ticker,
                                "question": m.get('title', ''),
                                "yes": prices[0] * 100,
                                "no": prices[1] * 100,
                                "volume": float(m.get('volume', 0)),
                                "liquidity": float(m.get('liquidity', 0))
                            }
                            markets.append(market_data)

                    # Store event with markets
                    if markets:
                        events_data[ticker] = {
                            **events_meta[ticker],
                            "markets": markets
                        }

            logger.info(f"Initial scan completed, found {len(events_meta)} Kalshi events")
            status["status"] = f"Initial scan completed, found {len(events_meta)} Kalshi events"
            status["last_update"] = time.time()
            save_kalshi_status(status)
        except Exception as e:
            logger.error(f"Error during initial Kalshi scan: {e}")
            status["status"] = f"Error during initial scan: {str(e)[:100]}"
            save_kalshi_status(status)

        while True:
            time.sleep(SCAN_INTERVAL)
            now = time.time()
            updated = {}
            new_moves = []
            sound_trigger = None

            try:
                status["status"] = "Scanning for updates"
                save_kalshi_status(status)

                for ev in fetch_all_events():
                    ticker = ev.get('ticker')
                    with scanner_lock:
                        # Update or create event metadata
                        meta = events_meta.setdefault(ticker, {
                            "ticker": ticker,
                            "title": ev.get('series_name', '') + ': ' + ev.get('title', ''),
                            "link": f"https://kalshi.com/events/{ticker}",
                            "volume": float(ev.get('volume', 0)),
                            "liquidity": float(ev.get('liquidity', 0))
                        })

                        markets = []
                        event_markets = fetch_markets_for_event(ticker)

                        for m in event_markets:
                            market_ticker = m.get('ticker')
                            new_prices = fetch_market_prices(market_ticker)

                            if not new_prices:
                                continue

                            # Compare with last known prices
                            old_prices = last_prices.get(market_ticker, new_prices)
                            yes_change = (new_prices[0] - old_prices[0]) * 100
                            no_change = (new_prices[1] - old_prices[1]) * 100

                            market_rec = {
                                "ticker": market_ticker,
                                "question": m.get('title', ''),
                                "yes": new_prices[0] * 100,
                                "no": new_prices[1] * 100,
                                "yd": abs(yes_change),
                                "nd": abs(no_change),
                                "ydir": "UP" if yes_change > 0 else ("DOWN" if yes_change < 0 else "—"),
                                "ndir": "UP" if no_change > 0 else ("DOWN" if no_change < 0 else "—"),
                                "max_move": max(abs(yes_change), abs(no_change)),
                                "volume": float(m.get('volume', 0)),
                                "volume24hr": float(m.get('volume_24h', 0)),
                                "liquidity": float(m.get('liquidity', 0))
                            }
                            markets.append(market_rec)

                            # Record move if significant change
                            if yes_change != 0 or no_change != 0:
                                move = {
                                    "time_ts": now,
                                    "event_ticker": ticker,
                                    "event_title": ev.get('series_name', '') + ': ' + ev.get('title', ''),
                                    "event_link": f"https://kalshi.com/events/{ticker}",
                                    "market_ticker": market_ticker,
                                    "question": m.get('title', ''),
                                    "max_move": max(abs(yes_change), abs(no_change)),
                                    "volume": float(m.get('volume', 0)),
                                    "liquidity": float(m.get('liquidity', 0)),
                                    "event_volume": float(ev.get('volume', 0)),
                                    **market_rec
                                }
                                recent_moves.append(move)
                                new_moves.append(move)

                            # Update last prices
                            last_prices[market_ticker] = new_prices

                        # Only update events with markets
                        if markets:
                            updated[ticker] = {
                                **meta,
                                "markets": markets
                            }

                with scanner_lock:
                    # Update events_data and save to file
                    if updated:
                        events_data = updated
                        save_kalshi_events_to_file(events_data)

                    # Save recent moves to file
                    save_kalshi_moves_to_file(recent_moves)

                # Determine sound trigger based on move magnitude
                if new_moves:
                    top = max(new_moves, key=lambda m: max(m.get("yd", 0), m.get("nd", 0)))
                    mag = max(top.get("yd", 0), top.get("nd", 0))
                    if mag > 5:
                        sound_trigger = {"level": "high", "magnitude": mag}
                    elif mag > 1:
                        sound_trigger = {"level": "medium", "magnitude": mag}
                    elif mag > 0.3:
                        sound_trigger = {"level": "low", "magnitude": mag}

                # Update status
                status["status"] = f"Updated {len(events_data)} Kalshi events with markets"
                status["last_update"] = now
                if sound_trigger:
                    status["sound_trigger"] = sound_trigger
                else:
                    status.pop("sound_trigger", None)
                save_kalshi_status(status)

                logger.info(f"Scanned {len(events_data)} Kalshi events, found {len(new_moves)} moves")

            except Exception as e:
                logger.error(f"Kalshi scan error: {e}")
                status["status"] = f"Scan error: {str(e)[:100]}"
                save_kalshi_status(status)

    @staticmethod
    def start_scanner():
        """Start the Kalshi scanner in a background thread"""
        threading.Thread(target=KalshiScannerService.scan_loop, daemon=True).start()

    @staticmethod
    def get_events_data():
        """Get current Kalshi events data (thread-safe)"""
        with scanner_lock:
            return events_data.copy()

    @staticmethod
    def get_recent_moves():
        """Get recent Kalshi moves data (thread-safe)"""
        with scanner_lock:
            return list(recent_moves)


def save_kalshi_events_to_file(events_data):
    """Save Kalshi events data to file"""
    try:
        with open(KALSHI_EVENTS_FILE, 'w') as f:
            import json
            json.dump(events_data, f)
        logger.info(f"Saved {len(events_data)} Kalshi events to file")
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi events to file: {e}")
        return False


def load_kalshi_events_from_file():
    """Load Kalshi events data from file"""
    try:
        if os.path.exists(KALSHI_EVENTS_FILE):
            with open(KALSHI_EVENTS_FILE, 'r') as f:
                import json
                data = json.load(f)
            logger.info(f"Loaded {len(data)} Kalshi events from file")
            return data
        return {}
    except Exception as e:
        logger.error(f"Error loading Kalshi events from file: {e}")
        return {}


def save_kalshi_moves_to_file(moves):
    """Save Kalshi recent moves to file"""
    try:
        moves_list = list(moves)
        with open(KALSHI_MOVES_FILE, 'w') as f:
            import json
            json.dump(moves_list, f)
        logger.info(f"Saved {len(moves_list)} Kalshi moves to file")
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi moves to file: {e}")
        return False


def load_kalshi_moves_from_file():
    """Load Kalshi recent moves from file"""
    try:
        if os.path.exists(KALSHI_MOVES_FILE):
            with open(KALSHI_MOVES_FILE, 'r') as f:
                import json
                data = json.load(f)
            logger.info(f"Loaded {len(data)} Kalshi moves from file")
            return deque(data, maxlen=MAX_MOVES)
        return deque(maxlen=MAX_MOVES)
    except Exception as e:
        logger.error(f"Error loading Kalshi moves from file: {e}")
        return deque(maxlen=MAX_MOVES)


def save_kalshi_status(status_data):
    """Save Kalshi status to file"""
    try:
        with open(KALSHI_STATUS_FILE, 'w') as f:
            import json
            json.dump(status_data, f)
        return True
    except Exception as e:
        logger.error(f"Error saving Kalshi status to file: {e}")
        return False


def load_kalshi_status():
    """Load Kalshi status from file"""
    try:
        if os.path.exists(KALSHI_STATUS_FILE):
            with open(KALSHI_STATUS_FILE, 'r') as f:
                import json
                return json.load(f)
        return {"last_update": 0, "status": "Not started"}
    except Exception as e:
        logger.error(f"Error loading Kalshi status from file: {e}")
        return {"last_update": 0, "status": "Error loading status"}


class KalshiScannerService:
    """Service to handle background scanning of Kalshi Elections data"""

    @staticmethod
    def scan_loop():
        """Main scanning loop to poll the Kalshi API"""
        global events_meta, events_data, last_prices, recent_moves

        logger.info("🔁 Kalshi Scan loop started")
        status = {"status": "Starting scan", "last_update": time.time()}
        save_kalshi_status(status)

        # Load existing data from files
        try:
            file_events = load_kalshi_events_from_file()
            if file_events:
                events_data = file_events
                events_meta = {
                    ticker: {"ticker": ticker, "title": ev["title"], "link": f"https://kalshi.com/events/{ticker}"}
                    for ticker, ev in events_data.items()}
                logger.info(f"Loaded {len(events_data)} Kalshi events from file")

            file_moves = load_kalshi_moves_from_file()
            if file_moves:
                recent_moves = file_moves
                logger.info(f"Loaded {len(recent_moves)} Kalshi moves from file")
        except Exception as e:
            logger.error(f"Error loading Kalshi data from files: {e}")

        # Initial seed
        try:
            status["status"] = "Initial scan"
            save_kalshi_status(status)

            for ev in fetch_all_events():
                ticker = ev.get('ticker')
                with scanner_lock:
                    events_meta[ticker] = {
                        "ticker": ticker,
                        "title": ev.get('title', ''),
                        "link": f"https://kalshi.com/events/{ticker}",
                        # Add event-level data if available
                        "volume": 0,  # Placeholder. Adjust based on Kalshi's actual volume data
                        "liquidity": 0  # Placeholder. Adjust based on Kalshi's liquidity data
                    }

                    # Process markets for the event
                    markets = []
                    for m in ev.get('markets', []):
                        market_ticker = m.get('ticker')
                        prices = parse_market_prices(m)

                        if prices:
                            last_prices[market_ticker] = prices

                            market_data = {
                                "ticker": market_ticker,
                                "question": m.get('question', ''),
                                "yes": prices[0] * 100,
                                "no": prices[1] * 100
                                # Add more market details as needed
                            }
                            markets.append(market_data)

                    # Store event with markets
                    if markets:
                        events_data[ticker] = {
                            **events_meta[ticker],
                            "markets": markets
                        }

            logger.info(f"Initial scan completed, found {len(events_meta)} Kalshi events")
            status["status"] = f"Initial scan completed, found {len(events_meta)} Kalshi events"
            status["last_update"] = time.time()
            save_kalshi_status(status)
        except Exception as e:
            logger.error(f"Error during initial Kalshi scan: {e}")
            status["status"] = f"Error during initial scan: {str(e)[:100]}"
            save_kalshi_status(status)

        while True:
            time.sleep(SCAN_INTERVAL)
            now = time.time()
            updated = {}
            new_moves = []
            sound_trigger = None

            try:
                status["status"] = "Scanning for updates"
                save_kalshi_status(status)

                for ev in fetch_all_events():
                    ticker = ev.get('ticker')
                    with scanner_lock:
                        # Update or create event metadata
                        meta = events_meta.setdefault(ticker, {
                            "ticker": ticker,
                            "title": ev.get('title', ''),
                            "link": f"https://kalshi.com/events/{ticker}",
                            "volume": 0,
                            "liquidity": 0
                        })

                        markets = []
                        for m in ev.get('markets', []):
                            market_ticker = m.get('ticker')
                            new_prices = parse_market_prices(m)

                            if not new_prices:
                                continue

                            # Compare with last known prices
                            old_prices = last_prices.get(market_ticker, new_prices)
                            yes_change = (new_prices[0] - old_prices[0]) * 100
                            no_change = (new_prices[1] - old_prices[1]) * 100

                            market_rec = {
                                "ticker": market_ticker,
                                "question": m.get('question', ''),
                                "yes": new_prices[0] * 100,
                                "no": new_prices[1] * 100,
                                "yd": abs(yes_change),
                                "nd": abs(no_change),
                                "ydir": "UP" if yes_change > 0 else ("DOWN" if yes_change < 0 else "—"),
                                "ndir": "UP" if no_change > 0 else ("DOWN" if no_change < 0 else "—"),
                                "max_move": max(abs(yes_change), abs(no_change)),
                                "volume": 0,  # Placeholder
                                "volume24hr": 0,  # Placeholder
                                "liquidity": 0  # Placeholder
                            }
                            markets.append(market_rec)

                            # Record move if significant change
                            if yes_change != 0 or no_change != 0:
                                move = {
                                    "time_ts": now,
                                    "event_ticker": ticker,
                                    "event_title": ev.get('title', ''),
                                    "event_link": f"https://kalshi.com/events/{ticker}",
                                    "market_ticker": market_ticker,
                                    "question": m.get('question', ''),
                                    "max_move": max(abs(yes_change), abs(no_change)),
                                    "volume": 0,  # Placeholder
                                    "liquidity": 0,  # Placeholder
                                    "event_volume": 0,  # Placeholder
                                    **market_rec
                                }
                                recent_moves.append(move)
                                new_moves.append(move)

                            # Update last prices
                            last_prices[market_ticker] = new_prices

                        # Only update events with markets
                        if markets:
                            updated[ticker] = {
                                **meta,
                                "markets": markets
                            }

                with scanner_lock:
                    # Update events_data and save to file
                    if updated:
                        events_data = updated
                        save_kalshi_events_to_file(events_data)

                    # Save recent moves to file
                    save_kalshi_moves_to_file(recent_moves)

                # Determine sound trigger based on move magnitude
                if new_moves:
                    top = max(new_moves, key=lambda m: max(m.get("yd", 0), m.get("nd", 0)))
                    mag = max(top.get("yd", 0), top.get("nd", 0))
                    if mag > 5:
                        sound_trigger = {"level": "high", "magnitude": mag}
                    elif mag > 1:
                        sound_trigger = {"level": "medium", "magnitude": mag}
                    elif mag > 0.3:
                        sound_trigger = {"level": "low", "magnitude": mag}

                # Update status
                status["status"] = f"Updated {len(events_data)} Kalshi events with markets"
                status["last_update"] = now
                if sound_trigger:
                    status["sound_trigger"] = sound_trigger
                else:
                    status.pop("sound_trigger", None)
                save_kalshi_status(status)

                logger.info(f"Scanned {len(events_data)} Kalshi events, found {len(new_moves)} moves")

            except Exception as e:
                logger.error(f"Kalshi scan error: {e}")
                status["status"] = f"Scan error: {str(e)[:100]}"
                save_kalshi_status(status)

    @staticmethod
    def start_scanner():
        """Start the Kalshi scanner in a background thread"""
        threading.Thread(target=KalshiScannerService.scan_loop, daemon=True).start()

    @staticmethod
    def get_events_data():
        """Get current Kalshi events data (thread-safe)"""
        with scanner_lock:
            return events_data.copy()

    @staticmethod
    def get_recent_moves():
        """Get recent Kalshi moves data (thread-safe)"""
        with scanner_lock:
            return list(recent_moves)