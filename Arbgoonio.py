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
                events_meta[eid] = {"id": eid, "title": ev["title"], "link": make_event_url(ev)}
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
        cutoff = now - (HISTORY_MINUTES * 60)  # Keep N-minute history
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
                        {"id": eid, "title": ev["title"], "link": make_event_url(ev)}
                    )
                    meta["title"] = ev["title"]

                    mkts = []
                    for m in ev.get("markets", []):
                        p = parse_prices(m.get("outcomePrices", []))
                        if not p:
                            continue

                        y, n = p
                        py, pn = last_prices.get(m["id"], (y, n))
                        dy, dn = (y - py) * 100, (n - pn) * 100

                        rec = {
                            "id": m["id"], "question": m["question"],
                            "yes": y * 100, "no": n * 100,
                            "yd": abs(dy), "nd": abs(dn),
                            "ydir": "UP" if dy > 0 else ("DOWN" if dy < 0 else "—"),
                            "ndir": "UP" if dn > 0 else ("DOWN" if dn < 0 else "—"),
                            "max_move": max(abs(dy), abs(dn))  # Store the biggest move for sorting
                        }
                        mkts.append(rec)

                        # record move
                        if dy != 0 or dn != 0:
                            move = {
                                "time_ts": now, "event_id": eid,
                                "event_title": ev["title"],
                                "event_link": make_event_url(ev),
                                "market_id": m["id"], "question": m["question"],
                                "max_move": max(abs(dy), abs(dn)),  # For sorting recent moves
                                **rec
                            }
                            recent_moves.append(move)
                            new_moves.append(move)

                        last_prices[m["id"]] = (y, n)

                    if mkts:  # Only add events with markets
                        updated[eid] = {**meta, "markets": mkts}

                # Automatically clean up events with no markets (reduces memory usage)
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
            # instead of trying to play it on the server
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


# ────────────────────────── TEMPLATES ──────────────────────────────
HOME_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Polymarket Live</title>
  <meta http-equiv="refresh" content="{{ refresh_interval }}">
  <style>
    body{background:#222;color:#ddd;font-family:sans-serif;padding:20px}
    a.btn,button.btn{display:inline-block;margin-right:10px;padding:6px 12px;
      background:#444;color:#ff69b4;text-decoration:none;border-radius:4px;cursor:pointer;}
    h1{margin-bottom:.5em}
    {{ BASE_STYLE }}
  </style>
  <script>
    // Function to load data from files
    async function loadData() {
      try {
        // Fetch status
        const statusRes = await fetch('/data/status');
        const statusData = await statusRes.json();
        document.getElementById('status').textContent = statusData.status;
        document.getElementById('last-update').textContent = Math.floor((Date.now()/1000) - statusData.last_update);

        // Check for sound trigger and play overlapping sounds if needed
        if (statusData.sound_trigger) {
          const magnitude = statusData.sound_trigger.magnitude;
          console.log("Sound trigger with magnitude:", magnitude);

          // Function to play sound with delay
          const playWithDelay = (soundFile, delay) => {
            setTimeout(() => {
              new Audio(soundFile).play().catch(e => console.log("Sound error:", e));
            }, delay);
          };

          // Play multiple sounds based on thresholds with overlapping
          if (magnitude > 5) {
            playWithDelay('/static/sound3.mp3', 1000); // High magnitude sound
          }
          if (magnitude > 1) {
            playWithDelay('/static/sound2.mp3', 500); // Medium magnitude sound with 0.25s delay
          }
          if (magnitude > 0.3) {
            playWithDelay('/static/sound1.mp3', 1); // Low magnitude sound with 0.5s delay
          }
        }

        // Fetch events
        const eventsRes = await fetch('/data/events');
        const eventsData = await eventsRes.json();

        // Convert to array and sort
        let events = Object.values(eventsData);
        document.getElementById('events-count').textContent = events.length;

        // Sort events if needed
        const sortByMove = {{ 'true' if sort_by_move else 'false' }};
        if (sortByMove) {
          // Simple sort by max move
          events.sort((a, b) => {
            const maxMoveA = Math.max(...a.markets.map(m => m.max_move || 0));
            const maxMoveB = Math.max(...b.markets.map(m => m.max_move || 0));
            return maxMoveB - maxMoveA;
          });
        }

        // Render events
        const container = document.getElementById('events-container');

        if (events.length === 0) {
          container.innerHTML = `
            <div style="text-align: center; margin-top: 50px; padding: 20px; background: #333;">
              <h3>No events with markets found</h3>
              <p>Status: ${statusData.status}</p>
              <p>Try clicking "Force Update" above.</p>
            </div>
          `;
          return;
        }

        let html = '';

        for (const ev of events) {
          html += `
            <h2>${ev.title}</h2>
            <a class="btn" href="${ev.link}" target="_blank">View Event</a>
            <table>
              <thead><tr><th>Market (ID)</th><th>YES</th><th>NO</th><th>Δ YES</th><th>Δ NO</th></tr></thead>
              <tbody>
          `;

          for (const m of ev.markets) {
            html += `
              <tr>
                <td class="market">${m.question} (${m.id})</td>
                <td class="yes">${m.yes.toFixed(2)}%</td>
                <td class="no">${m.no.toFixed(2)}%</td>
                <td class="${m.ydir.toLowerCase()} ${
                  m.yd > 5 ? 'highlight-5' : (m.yd > 1 ? 'highlight-1' : (m.yd > 0.3 ? 'highlight-0-3' : ''))
                }">
                  ${m.ydir} ${m.yd.toFixed(2)}%
                </td>
                <td class="${m.ndir.toLowerCase()} ${
                  m.nd > 5 ? 'highlight-5' : (m.nd > 1 ? 'highlight-1' : (m.nd > 0.3 ? 'highlight-0-3' : ''))
                }">
                  ${m.ndir} ${m.nd.toFixed(2)}%
                </td>
              </tr>
            `;
          }

          html += `
              </tbody>
            </table>
          `;
        }

        container.innerHTML = html;

      } catch (error) {
        console.error('Error loading data:', error);
        document.getElementById('status').textContent = `Error: ${error.message}`;
      }
    }

    // Load data when page loads
    document.addEventListener('DOMContentLoaded', loadData);
  </script>
</head>
<body>
  <h1>Polymarket Live Scanner</h1>

  <div class="status-bar">
    <div>
      Status: <span id="status">Loading...</span> | 
      Events: <span id="events-count">0</span> | 
      Last update: <span id="last-update">--</span>s ago
    </div>
    <div>
      <a href="/debug" target="_blank">Debug</a> | 
      <a href="/fetch">Force Update</a>
    </div>
  </div>

  <div class="btn-container">
    <div>
      <button id="enable-sound" class="btn">Enable Sounds</button>
      <a class="btn" href="/recent">Recent Moves</a>
    </div>
    <div class="right-align">
      <button onclick="location='/?sort=move';" class="btn">Sort by Biggest Move</button>
    </div>
  </div>
  <p>Next refresh in <span id="timer">--</span>s</p>

  <div id="events-container">
    <div style="text-align: center; padding: 20px;">
      <p>Loading events data...</p>
    </div>
  </div>

  <script>
    /* countdown */
    let t={{ refresh_interval }};
    setInterval(()=>{
      t=t? t-1:{{ refresh_interval }};
      document.getElementById('timer').textContent=t;

      // Update last update counter
      const lastUpdate = document.getElementById('last-update');
      if (lastUpdate.textContent !== '--') {
        lastUpdate.textContent = parseInt(lastUpdate.textContent) + 1;
      }
    },1000);

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

RECENT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Recent Moves</title>
  <meta http-equiv="refresh" content="{{ refresh_interval }}">
  <style>
    body{background:#222;color:#ddd;font-family:sans-serif;padding:20px}
    a.btn,button.btn{display:inline-block;margin-right:10px;padding:6px 12px;
      background:#444;color:#ff69b4;text-decoration:none;border-radius:4px;cursor:pointer;}
    h1{margin-bottom:.5em}
    {{ BASE_STYLE }}
  </style>
  <script>
    // Function to load data from files
    async function loadData() {
      try {
        // Fetch status
        const statusRes = await fetch('/data/status');
        const statusData = await statusRes.json();
        document.getElementById('status').textContent = statusData.status;
        document.getElementById('last-update').textContent = Math.floor((Date.now()/1000) - statusData.last_update);

        // Check for sound trigger and play overlapping sounds if needed
        if (statusData.sound_trigger) {
          const magnitude = statusData.sound_trigger.magnitude;
          console.log("Sound trigger with magnitude:", magnitude);

          // Function to play sound with delay
          const playWithDelay = (soundFile, delay) => {
            setTimeout(() => {
              new Audio(soundFile).play().catch(e => console.log("Sound error:", e));
            }, delay);
          };

          // Play multiple sounds based on thresholds with overlapping
          if (magnitude > 5) {
            playWithDelay('/static/sound3.mp3', 1000); // High magnitude sound
          }
          if (magnitude > 1) {
            playWithDelay('/static/sound2.mp3', 500); // Medium magnitude sound with 0.25s delay
          }
          if (magnitude > 0.3) {
            playWithDelay('/static/sound1.mp3', 1); // Low magnitude sound with 0.5s delay
          }
        }

        // Fetch recent moves
        const movesRes = await fetch('/data/moves');
        const allMoves = await movesRes.json();

        // Filter recent moves (last N minutes)
        const now = Date.now() / 1000;
        const cutoff = now - ({{ history_minutes }} * 60);
        const moves = allMoves.filter(m => m.time_ts >= cutoff);

        document.getElementById('moves-count').textContent = moves.length;

        // Sort moves if needed
        const sortByMove = {{ 'true' if sort_by_move else 'false' }};
        if (sortByMove) {
          moves.sort((a, b) => b.max_move - a.max_move);
        } else {
          moves.sort((a, b) => b.time_ts - a.time_ts);
        }

        // Render moves
        const container = document.getElementById('moves-container');

        if (moves.length === 0) {
          container.innerHTML = `
            <div style="text-align: center; margin-top: 50px; padding: 20px; background: #333;">
              <h3>No recent price movements detected</h3>
              <p>Price changes will appear here as they occur.</p>
            </div>
          `;
          return;
        }

        let html = `
          <table>
            <thead><tr>
              <th>Since</th><th>Event</th><th>Market (ID)</th>
              <th>YES</th><th>NO</th><th>Δ YES</th><th>Δ NO</th>
            </tr></thead>
            <tbody>
        `;

        for (const m of moves) {
          const diff = now - m.time_ts;
          const ago = diff < 60 ? `${Math.round(diff)}s` : `${(diff/60).toFixed(1)}m`;

          html += `
            <tr>
              <td>${ago}</td>
              <td><a class="btn" href="${m.event_link}" target="_blank">${m.event_title}</a></td>
              <td class="market">${m.question} (${m.market_id})</td>
              <td class="yes">${m.yes.toFixed(2)}%</td>
              <td class="no">${m.no.toFixed(2)}%</td>
              <td class="${m.ydir.toLowerCase()} ${
                m.yd > 5 ? 'highlight-5' : (m.yd > 1 ? 'highlight-1' : (m.yd > 0.3 ? 'highlight-0-3' : ''))
              }">
                ${m.ydir} ${m.yd.toFixed(2)}%
              </td>
              <td class="${m.ndir.toLowerCase()} ${
                m.nd > 5 ? 'highlight-5' : (m.nd > 1 ? 'highlight-1' : (m.nd > 0.3 ? 'highlight-0-3' : ''))
              }">
                ${m.ndir} ${m.nd.toFixed(2)}%
              </td>
            </tr>
          `;
        }

        html += `
            </tbody>
          </table>
        `;

        container.innerHTML = html;

      } catch (error) {
        console.error('Error loading data:', error);
        document.getElementById('status').textContent = `Error: ${error.message}`;
      }
    }

    // Load data when page loads
    document.addEventListener('DOMContentLoaded', loadData);
  </script>
</head>
<body>
  <h1>Recent Moves (last {{ history_minutes }} min)</h1>

  <div class="status-bar">
    <div>
      Status: <span id="status">Loading...</span> | 
      Moves: <span id="moves-count">0</span> | 
      Last update: <span id="last-update">--</span>s ago
    </div>
    <div>
      <a href="/debug" target="_blank">Debug</a> | 
      <a href="/fetch">Force Update</a>
    </div>
  </div>

  <div class="btn-container">
    <div>
      <button id="enable-sound" class="btn">Enable Sounds</button>
      <a class="btn" href="/">Home</a>
    </div>
    <div class="right-align">
      <button onclick="location='/recent{% if not sort_by_move %}?sort=move{% endif %}';" class="btn">
        {% if sort_by_move %}
          Sort by Time
        {% else %}
          Sort by Biggest Move
        {% endif %}
      </button>
    </div>
  </div>
  <p>Next refresh in <span id="timer">--</span>s</p>

  <div id="moves-container">
    <div style="text-align: center; padding: 20px;">
      <p>Loading recent moves data...</p>
    </div>
  </div>

  <script>
    /* countdown */
    let t={{ refresh_interval }};
    setInterval(()=>{
      t=t? t-1:{{ refresh_interval }};
      document.getElementById('timer').textContent=t;

      // Update last update counter
      const lastUpdate = document.getElementById('last-update');
      if (lastUpdate.textContent !== '--') {
        lastUpdate.textContent = parseInt(lastUpdate.textContent) + 1;
      }
    },1000);

    /* unlock sounds here too */
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
    return json.dumps({"name": "Polymarket Scanner", "short_name": "PolyScanner"})


@app.route('/service-worker.js')
def service_worker():
    """Serve empty service worker to avoid 404s"""
    return "", 200


@app.route("/")
def index():
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"User access from IP: {user_ip}")
    """Main page showing all events"""
    return render_template_string(
        HOME_TEMPLATE,
        BASE_STYLE=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL,
        sort_by_move="sort" in request.args
    )


@app.route("/recent")
def recent():
    """Page showing recent price movements"""
    return render_template_string(
        RECENT_TEMPLATE,
        BASE_STYLE=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL,
        history_minutes=HISTORY_MINUTES,
        sort_by_move="sort" in request.args
    )


# ────────────────────────── STARTUP ────────────────────────────────
# Start the scanner thread - this must be outside the __main__ check for Render
start_scanner()

# For local development only - Render.com handles this part automatically
if __name__ == "__main__":
    app.run(debug=False)