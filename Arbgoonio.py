import os
import time
import threading
import json
import requests
import logging
from collections import deque
from flask import Flask, render_template_string, request, send_file
from slugify import slugify
from playsound import playsound  # pip install playsound==1.3.0

# ────────────────────────── CONFIGURATION ──────────────────────────
API_BASE = "https://gamma-api.polymarket.com"
SCAN_INTERVAL = 10  # seconds between API polls
REFRESH_INTERVAL = 5  # seconds between browser refreshes
HISTORY_MINUTES = 5  # minutes to keep price movement history
MAX_MOVES = 500  # maximum number of moves to store (memory optimization)

# File paths for persistent storage
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
EVENTS_FILE = os.path.join(DATA_DIR, "events_data.json")
MOVES_FILE = os.path.join(DATA_DIR, "recent_moves.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ────────────────────────── APP & STATE ────────────────────────────
app = Flask(__name__)
lock = threading.RLock()  # For thread safety

# Use more efficient data structures
events_meta = {}  # id -> {id,title,link}
events_data = {}  # id -> {id,title,link,markets:[…]}
last_prices = {}  # market_id -> (yes,no)
recent_moves = deque(maxlen=MAX_MOVES)  # Fixed-size queue


# ────────────────────────── FILE OPERATIONS ─────────────────────────
def save_events_to_file(events_data):
    """Save events data to a file"""
    try:
        with open(EVENTS_FILE, 'w') as f:
            json.dump(events_data, f)
        logger.info(f"Saved {len(events_data)} events to file")
        return True
    except Exception as e:
        logger.error(f"Error saving events to file: {e}")
        return False


def load_events_from_file():
    """Load events data from file"""
    try:
        if os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE, 'r') as f:
                data = json.load(f)
            logger.info(f"Loaded {len(data)} events from file")
            return data
        return {}
    except Exception as e:
        logger.error(f"Error loading events from file: {e}")
        return {}


def save_moves_to_file(moves):
    """Save recent moves to a file"""
    try:
        moves_list = list(moves)
        with open(MOVES_FILE, 'w') as f:
            json.dump(moves_list, f)
        logger.info(f"Saved {len(moves_list)} moves to file")
        return True
    except Exception as e:
        logger.error(f"Error saving moves to file: {e}")
        return False


def load_moves_from_file():
    """Load recent moves from file"""
    try:
        if os.path.exists(MOVES_FILE):
            with open(MOVES_FILE, 'r') as f:
                data = json.load(f)
            logger.info(f"Loaded {len(data)} moves from file")
            # Convert back to deque
            return deque(data, maxlen=MAX_MOVES)
        return deque(maxlen=MAX_MOVES)
    except Exception as e:
        logger.error(f"Error loading moves from file: {e}")
        return deque(maxlen=MAX_MOVES)


def save_status(status_data):
    """Save status information to file"""
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status_data, f)
        return True
    except Exception as e:
        logger.error(f"Error saving status to file: {e}")
        return False


def load_status():
    """Load status information from file"""
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        return {"last_update": 0, "status": "Not started"}
    except Exception as e:
        logger.error(f"Error loading status from file: {e}")
        return {"last_update": 0, "status": "Error loading status"}


# ────────────────────────── SHARED CSS ─────────────────────────────
BASE_STYLE = """
table{
    width:100%;border-collapse:collapse;table-layout:fixed;
}
th,td{
    padding:8px;border-bottom:1px solid rgba(85,85,85,.8);
    word-wrap:break-word;vertical-align:top;
    text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000;
}
th:first-child,td:first-child{text-align:left;}
th:not(:first-child),td:not(:first-child){text-align:right;}
.market{color:#fff;}
.yes   {color:#0f0;font-weight:bold;}
.no    {color:#f00;font-weight:bold;}
.up    {color:#0f0;}
.down  {color:#f00;}
.highlight-0-3{background:rgba(254,254,51,.9);}
.highlight-1  {background:rgba(102,204,255,.9);}
.highlight-5  {background:rgba(251,154,181,.9);}
.right-align{
    float:right;
    text-align:right;
}
.btn-container{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:10px;
}
.status-bar{
    background:#333;
    padding:5px 10px;
    margin-bottom:10px;
    display:flex;
    justify-content:space-between;
}
"""


# ────────────────────────── API HELPERS ────────────────────────────
def fetch_all_events(params=None, page_size=100, max_retries=3):
    params = params or {}
    out, offset = [], 0

    while True:
        q = params.copy()
        q.update(limit=page_size, offset=offset)

        for attempt in range(max_retries):
            try:
                r = requests.get(f"{API_BASE}/events", params=q, timeout=30)
                r.raise_for_status()
                batch = r.json()

                if not batch:
                    break

                out.extend(batch)

                if len(batch) < page_size:
                    break

                offset += page_size
                break  # Success, exit retry loop

            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    logger.error(f"API request failed after {max_retries} attempts: {e}")
                    return out
                logger.warning(f"API request failed (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(2)  # Wait before retry

        if not batch or len(batch) < page_size:
            break

    return out


def make_event_url(ev):
    tid = ev["id"]
    slug = ev.get("slug") or slugify(ev["title"])
    return f"https://polymarket.com/event/{slug}?tid={tid}"


def parse_prices(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None

    try:
        return float(raw[0]), float(raw[1])
    except (IndexError, TypeError, ValueError):
        return None


# ────────────────────────── SOUND HELPER ───────────────────────────
def play_sound_async(path):
    try:
        threading.Thread(target=lambda: playsound(path), daemon=True).start()
    except Exception as e:
        logger.warning(f"Failed to play sound: {e}")


# ────────────────────────── BACKGROUND SCANNER ─────────────────────
def scan_loop():
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
            recent_moves = file_moves
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
    threading.Thread(target=scan_loop, daemon=True).start()


# ────────────────────────── ROUTES FOR FILE ACCESS ──────────────────
@app.route('/data/events')
def get_events_data():
    """Serve events data from file"""
    if os.path.exists(EVENTS_FILE):
        return send_file(EVENTS_FILE, mimetype='application/json')
    return json.dumps({})


@app.route('/data/moves')
def get_moves_data():
    """Serve moves data from file"""
    if os.path.exists(MOVES_FILE):
        return send_file(MOVES_FILE, mimetype='application/json')
    return json.dumps([])


@app.route('/data/status')
def get_status_data():
    """Serve status data from file"""
    if os.path.exists(STATUS_FILE):
        return send_file(STATUS_FILE, mimetype='application/json')
    return json.dumps({"last_update": 0, "status": "Unknown"})


@app.route('/fetch')
def manual_fetch():
    """Manually trigger a data fetch"""
    threading.Thread(target=scan_loop, daemon=True).start()
    return "Fetch initiated"


@app.route('/debug')
def debug():
    """Debug endpoint for checking file status"""
    status = {
        "events_file_exists": os.path.exists(EVENTS_FILE),
        "events_file_size": os.path.getsize(EVENTS_FILE) if os.path.exists(EVENTS_FILE) else 0,
        "moves_file_exists": os.path.exists(MOVES_FILE),
        "moves_file_size": os.path.getsize(MOVES_FILE) if os.path.exists(MOVES_FILE) else 0,
        "status_file_exists": os.path.exists(STATUS_FILE),
        "status": load_status()
    }
    return json.dumps(status)


# ────────────────────────── TERMINAL TEMPLATE ─────────────────────────
TERMINAL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Arbgoon terminal.</title>
  <style>
    body{
      background:#111;
      color:#ddd;
      font-family:monospace;
      padding:20px;
      margin:0;
      font-size:14px;
    }

    .terminal-container {
      background:#000;
      border:1px solid #333;
      border-radius:5px;
      padding:10px;
      height:85vh;
      overflow-y:auto;
      position:relative;
    }

    .terminal-header {
      position:sticky;
      top:0;
      background:#000;
      border-bottom:1px solid #333;
      padding-bottom:10px;
      margin-bottom:10px;
      z-index:100;
    }

    .terminal-title {
      color:#0f0;
      font-weight:bold;
      margin-bottom:5px;
    }

    .status-bar {
      display:flex;
      justify-content:space-between;
      align-items:center;
      color:#0f0;
      font-size:12px;
      margin-bottom:10px;
    }

    .terminal-row {
      display:flex;
      padding:8px 5px;
      border-bottom:1px solid #222;
      transition:background 0.2s;
      align-items:center;
    }

    .terminal-row:hover {
      background:#1a1a1a;
    }

    .terminal-row > div {
      padding-right:15px;
    }

    .event-time {
      width:60px;
      color:#888;
    }

    .event-button {
      background:#222;
      color:#0f0;
      border:1px solid #0f0;
      border-radius:3px;
      padding:3px 8px;
      cursor:pointer;
      text-decoration:none;
      font-family:monospace;
      font-size:12px;
      display:inline-block;
    }

    .event-button:hover {
      background:#0f0;
      color:#000;
    }

    .market {
      flex-grow:1;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }

    .yes-price, .no-price {
      width:70px;
      text-align:right;
    }

    .yes-price {
      color:#0f0;
    }

    .no-price {
      color:#f55;
    }

    .yes-change, .no-change {
      width:80px;
      text-align:right;
    }

    .up {
      color:#0f0;
    }

    .down {
      color:#f55;
    }

    .volume {
      width:80px;
      text-align:right;
      color:#0af;
    }

    .event-volume {
      width:80px;
      text-align:right;
      color:#f0f;
    }

    .highlight-row {
      animation: highlight-flash 2s;
    }

    @keyframes highlight-flash {
      0%, 100% { background: #1a1a1a; }
      50% { background: #332200; }
    }

    .menu-container {
      display:flex;
      justify-content:space-between;
      margin-bottom:15px;
    }

    .menu-button {
      background:#222;
      color:#0f0;
      border:1px solid #0f0;
      border-radius:3px;
      padding:5px 10px;
      cursor:pointer;
      margin-right:10px;
      font-family:monospace;
      text-decoration:none;
    }

    .menu-button:hover {
      background:#0f0;
      color:#000;
    }

    .moves-count {
      color:#0af;
    }

    /* Hide scrollbar but allow scrolling */
    .terminal-container::-webkit-scrollbar {
      width: 0px;
      background: transparent;
    }

    /* For Firefox */
    .terminal-container {
      scrollbar-width: none;
    }

    .loader {
      color: #0f0;
      font-weight: bold;
      animation: blink 1s step-end infinite;
    }

    @keyframes blink {
      50% { opacity: 0; }
    }

    .market-meta {
      font-size:11px;
      color:#666;
      margin-top:2px;
    }

    /* Filter controls */
    .filter-controls {
      display: flex;
      align-items: center;
      margin-top: 10px;
    }

    .filter-label {
      margin-right: 10px;
      color: #888;
    }

    .filter-input {
      background: #111;
      color: #0f0;
      border: 1px solid #333;
      padding: 3px 5px;
      font-family: monospace;
      width: 50px;
      margin-right: 10px;
    }

    {{ BASE_STYLE }}
  </style>
  <script>
    // Global variables to track state
    let lastUpdateTime = 0;
    let pollingInterval;
    let countdownInterval;
    let allMoves = [];
    let filteredMoves = [];
    let minMoveFilter = 0.1; // Default minimum move filter (0.1%)

    // Function to format numbers with K/M suffix
    function formatNumberCompact(num) {
      if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
      } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
      } else {
        return num.toFixed(2);
      }
    }

    // Format timestamp to HH:MM:SS
    function formatTime(timestamp) {
      const date = new Date(timestamp * 1000);
      const hours = date.getHours().toString().padStart(2, '0');
      const minutes = date.getMinutes().toString().padStart(2, '0');
      const seconds = date.getSeconds().toString().padStart(2, '0');
      return `${hours}:${minutes}:${seconds}`;
    }

    // Calculate how long ago in a compact format
    function timeAgo(timestamp) {
      const now = Date.now() / 1000;
      const diff = now - timestamp;

      if (diff < 60) {
        return Math.round(diff) + 's';
      } else if (diff < 3600) {
        return Math.round(diff / 60) + 'm';
      } else {
        return Math.round(diff / 3600) + 'h';
      }
    }

    // Function to load data from files
    async function loadData() {
      try {
        // Fetch status
        const statusRes = await fetch('/data/status');
        const statusData = await statusRes.json();
        document.getElementById('status').textContent = statusData.status;
        document.getElementById('last-update').textContent = Math.floor((Date.now()/1000) - statusData.last_update);

        // Only update if the server data is newer
        if (statusData.last_update > lastUpdateTime) {
          lastUpdateTime = statusData.last_update;

          // Play sound if significant move detected
          if (statusData.sound_trigger) {
            const magnitude = statusData.sound_trigger.magnitude;
            const playCount = statusData.sound_trigger.playCount || 1; // Default to 1 if not specified

            // Function to play sound multiple times with delay
            const playMultipleTimes = (soundFile, count, baseDelay = 300) => {
              for (let i = 0; i < count; i++) {
                // Each subsequent play is delayed by 500ms more than the previous
                const delay = baseDelay + (i * 250);
                setTimeout(() => {
                  new Audio(soundFile).play().catch(e => console.log("Sound error:", e));
                }, delay);
              }
            };

            // Play sounds based on thresholds, potentially multiple times
            if (magnitude > 5) {
              playMultipleTimes('/static/sound3.mp3', playCount, 1000);
            }
            if (magnitude > 1) {
              playMultipleTimes('/static/sound2.mp3', playCount, 500);
            }
            if (magnitude > 0.3) {
              playMultipleTimes('/static/sound1.mp3', playCount, 250);
            }
          }

          // Fetch recent moves
          const movesRes = await fetch('/data/moves');
          allMoves = await movesRes.json();

          // Apply filters
          applyFilters();

          document.getElementById('moves-count').textContent = filteredMoves.length;
        }
      } catch (error) {
        console.error('Error loading data:', error);
        document.getElementById('status').textContent = `Error: ${error.message}`;
      }
    }

    // Apply filters and render moves
    function applyFilters() {
      const now = Date.now() / 1000;
      const cutoff = now - ({{ history_minutes }} * 60);
      
      // Get current filter value
      minMoveFilter = parseFloat(document.getElementById('min-move-filter').value) || 0.1;
      
      // Filter moves based on cutoff time and minimum move
      let validMoves = allMoves.filter(m => m.time_ts >= cutoff && m.max_move >= minMoveFilter);
      
      // Group moves by timestamp (to group batches)
      const movesGroupedByTime = {};
      validMoves.forEach(move => {
        // Round to nearest second to group moves from same batch
        // This handles slight time differences in the same batch
        const roundedTime = Math.round(move.time_ts);
        if (!movesGroupedByTime[roundedTime]) {
          movesGroupedByTime[roundedTime] = [];
        }
        movesGroupedByTime[roundedTime].push(move);
      });
      
      // Sort timestamps descending (newest first)
      const sortedTimes = Object.keys(movesGroupedByTime)
        .map(Number)
        .sort((a, b) => b - a);
      
      // Create final sorted array
      filteredMoves = [];
      sortedTimes.forEach(timestamp => {
        // Sort moves within each timestamp batch by max_move (largest first)
        const sortedBatch = movesGroupedByTime[timestamp].sort((a, b) => b.max_move - a.max_move);
        // Add sorted batch to filtered moves
        filteredMoves = filteredMoves.concat(sortedBatch);
      });
      
      renderMoves();
    }

    // Render the moves to the terminal
    function renderMoves() {
      const container = document.getElementById('terminal-content');

      if (filteredMoves.length === 0) {
        container.innerHTML = `
          <div style="text-align: center; padding: 20px; color: #666;">
            <p>No moves detected matching current filters</p>
            <p>Waiting for market activity...</p>
            <p class="loader">_</p>
          </div>
        `;
        return;
      }

      let html = '';
      const existingRows = new Set(Array.from(container.children).map(el => el.dataset.id));
      const newRows = new Set();

      // Build HTML for all moves
      for (const m of filteredMoves) {
        const rowId = `move-${m.market_id}-${m.time_ts}`;
        newRows.add(rowId);

        const marketVolume = formatNumberCompact(m.volume || 0);
        const eventVolume = formatNumberCompact(m.event_volume || 0);

        // Format change indicators with arrows
        const yesChange = `${m.ydir === 'UP' ? '▲' : (m.ydir === 'DOWN' ? '▼' : '—')} ${m.yd.toFixed(2)}%`;
        const noChange = `${m.ndir === 'UP' ? '▲' : (m.ndir === 'DOWN' ? '▼' : '—')} ${m.nd.toFixed(2)}%`;

        const isNewRow = !existingRows.has(rowId);
        const highlightClass = isNewRow ? 'highlight-row' : '';

        html += `
          <div class="terminal-row ${highlightClass}" data-id="${rowId}">
            <div class="event-time">${formatTime(m.time_ts)}</div>
            <div>
              <a href="${m.event_link}" target="_blank" class="event-button">GO</a>
            </div>
            <div class="market">
              ${m.question}
              <div class="market-meta">
                ${m.event_title.substring(0, 40)}${m.event_title.length > 40 ? '...' : ''}
              </div>
            </div>
            <div class="yes-price">${m.yes.toFixed(1)}%</div>
            <div class="no-price">${m.no.toFixed(1)}%</div>
            <div class="yes-change ${m.ydir.toLowerCase()}">${yesChange}</div>
            <div class="no-change ${m.ndir.toLowerCase()}">${noChange}</div>
            <div class="volume">$${marketVolume}</div>
            <div class="event-volume">${eventVolume}</div>
          </div>
        `;
      }

      // If container is empty, just set innerHTML
      if (container.children.length === 0) {
        container.innerHTML = html;
      } else {
        // Otherwise, maintain existing rows and add new ones at the top
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = html;

        // Remove rows that don't exist anymore
        Array.from(container.children).forEach(child => {
          if (!newRows.has(child.dataset.id)) {
            container.removeChild(child);
          }
        });

        // Add new rows and update existing ones
        Array.from(tempDiv.children).forEach(newChild => {
          const existingChild = container.querySelector(`[data-id="${newChild.dataset.id}"]`);
          if (existingChild) {
            // Update existing row content if needed
            existingChild.innerHTML = newChild.innerHTML;
          } else {
            // Insert new row at the top
            if (container.firstChild) {
              container.insertBefore(newChild, container.firstChild);
            } else {
              container.appendChild(newChild);
            }
          }
        });
      }
    }

    // Function to start auto-refresh polling
    function startAutoRefresh(intervalSeconds) {
      // Clear any existing intervals
      if (pollingInterval) clearInterval(pollingInterval);
      if (countdownInterval) clearInterval(countdownInterval);

      // Set polling interval for data refresh
      pollingInterval = setInterval(() => {
        loadData();
      }, intervalSeconds * 1000);

      // Set countdown display
      let countdown = intervalSeconds;
      document.getElementById('timer').textContent = countdown;

      countdownInterval = setInterval(() => {
        countdown = countdown > 0 ? countdown - 1 : intervalSeconds;
        document.getElementById('timer').textContent = countdown;

        // Update last update counter
        const lastUpdate = document.getElementById('last-update');
        if (lastUpdate.textContent !== '--') {
          lastUpdate.textContent = parseInt(lastUpdate.textContent) + 1;
        }
      }, 1000);
    }

    // Initialize data loading and auto-refresh when page loads
    document.addEventListener('DOMContentLoaded', () => {
      // Load data immediately
      loadData();

      // Start auto-refresh with interval
      startAutoRefresh({{ refresh_interval }});

      // Set up filter input event
      document.getElementById('min-move-filter').addEventListener('change', applyFilters);
      document.getElementById('apply-filter').addEventListener('click', applyFilters);
    });
  </script>
</head>
<body>
  <div class="menu-container">
    <div>
      <button id="enable-sound" class="menu-button">Enable Sounds</button>
      <a href="/fetch" onclick="loadData(); return false;" class="menu-button">Force Update</a>
      <a href="/debug" target="_blank" class="menu-button">Debug</a>
    </div>
  </div>

  <div class="terminal-container">
    <div class="terminal-header">
      <div class="terminal-title">Ouro is gay</div>
      <div class="status-bar">
        <div>
          Status: <span id="status">Loading...</span> | 
          Moves: <span id="moves-count">0</span> | 
          Last update: <span id="last-update">--</span>s ago |
          Next update: <span id="timer">--</span>s
        </div>
      </div>

      <div class="filter-controls">
        <span class="filter-label">Min move %:</span>
        <input type="number" id="min-move-filter" class="filter-input" value="0.1" step="0.1" min="0">
        <button id="apply-filter" class="menu-button">Apply</button>
      </div>
    </div>

    <div id="terminal-content">
      <div style="text-align: center; padding: 20px; color: #666;">
        <p>Loading terminal data...</p>
        <p class="loader">_</p>
      </div>
    </div>
  </div>

  <script>
    /* unlock sounds */
    const s0=new Audio('/static/sound1.mp3'),
          s1=new Audio('/static/sound2.mp3'),
          s5=new Audio('/static/sound3.mp3');
    document.getElementById('enable-sound').onclick=()=>{
      [s0,s1,s5].forEach(a=>{a.play().catch(()=>{});a.pause();a.currentTime=0;});
    };
  </script>
</body>
</html>
"""

# ────────────────────────── ROUTES ─────────────────────────────────
@app.route('/manifest.json')
def manifest():
    """Serve empty manifest to avoid 404s"""
    return json.dumps({"name": "Polymarket Terminal", "short_name": "PolyTerminal"})


@app.route('/service-worker.js')
def service_worker():
    """Serve empty service worker to avoid 404s"""
    return "", 200


# Make the terminal the default route
@app.route("/")
def index():
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"User access from IP: {user_ip}")
    """Terminal page is now the main page"""
    return render_template_string(
        TERMINAL_TEMPLATE,
        BASE_STYLE=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL,
        history_minutes=HISTORY_MINUTES
    )


# ────────────────────────── STARTUP ────────────────────────────────
# Start the scanner thread - this must be outside the __main__ check for Render
start_scanner()

# For local development
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)