import os
import time
import threading
import json
import requests
import logging
from collections import deque
from flask import Flask, render_template_string, request, jsonify, send_from_directory

# Ensure slugify is imported in a way that works with Render
try:
    from slugify import slugify
except ImportError:
    # Fallback implementation if the library isn't available
    def slugify(text):
        import re
        text = text.lower()
        return re.sub(r'[^a-z0-9]+', '-', text).strip('-')

# Check if playsound is available (won't be used on Render but kept for local dev)
try:
    from playsound import playsound
except ImportError:
    # Create a dummy implementation if not available
    def playsound(path):
        pass

# ────────────────────────── CONFIGURATION ──────────────────────────
API_BASE = "https://gamma-api.polymarket.com"
SCAN_INTERVAL = 10  # seconds between API polls
REFRESH_INTERVAL = 5  # seconds between browser refreshes
HISTORY_MINUTES = 5  # minutes to keep price movement history
MAX_MOVES = 500  # maximum number of moves to store

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ────────────────────────── APP & STATE ────────────────────────────
app = Flask(__name__)

# Use global variables with proper locks for thread safety
lock = threading.RLock()
events_data = {}  # Main events data store
events_meta = {}  # Metadata about events
last_prices = {}  # Last known prices
recent_moves = deque(maxlen=MAX_MOVES)  # Recent price movements

# For debugging, track when we last updated the data
last_data_update = 0
scanner_status = "Not started"


# ────────────────────────── API HELPERS ────────────────────────────
def fetch_all_events(params=None, page_size=50, max_retries=3):
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

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"API request failed: {e}")
                    return out
                logger.warning(f"API request attempt {attempt + 1} failed: {e}")
                time.sleep(1)  # Wait before retry

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


# ────────────────────────── DATA PROCESSING ──────────────────────────
def process_events(params=None):
    """Process events data and update global state"""
    global events_data, events_meta, last_prices, recent_moves, last_data_update, scanner_status

    params = params or {"closed": False, "archived": False, "active": True}

    try:
        scanner_status = "Fetching events"
        raw_events = fetch_all_events(params)
        logger.info(f"Fetched {len(raw_events)} raw events")

        if not raw_events:
            logger.warning("No events received from API")
            scanner_status = "No events found"
            return

        now = time.time()
        updated = {}
        new_moves = []

        scanner_status = "Processing events"
        for ev in raw_events:
            eid = ev["id"]

            # Get or create metadata
            with lock:
                meta = events_meta.setdefault(
                    eid,
                    {"id": eid, "title": ev["title"], "link": make_event_url(ev)}
                )
                meta["title"] = ev["title"]  # Update title in case it changed

                # Process markets for this event
                markets = []
                has_active_markets = False

                for m in ev.get("markets", []):
                    # Parse prices
                    prices = parse_prices(m.get("outcomePrices"))
                    if not prices:
                        continue

                    has_active_markets = True
                    y, n = prices

                    # Calculate price changes
                    py, pn = last_prices.get(m["id"], (y, n))
                    dy, dn = (y - py) * 100, (n - pn) * 100

                    # Create record for this market
                    market_record = {
                        "id": m["id"],
                        "question": m["question"],
                        "yes": y * 100,
                        "no": n * 100,
                        "yd": abs(dy),
                        "nd": abs(dn),
                        "ydir": "UP" if dy > 0 else ("DOWN" if dy < 0 else "—"),
                        "ndir": "UP" if dn > 0 else ("DOWN" if dn < 0 else "—"),
                        "max_move": max(abs(dy), abs(dn))
                    }
                    markets.append(market_record)

                    # Record price movements
                    if abs(dy) > 0.01 or abs(dn) > 0.01:  # Only record non-trivial changes
                        move = {
                            "time_ts": now,
                            "event_id": eid,
                            "event_title": ev["title"],
                            "event_link": make_event_url(ev),
                            "market_id": m["id"],
                            "question": m["question"],
                            "max_move": max(abs(dy), abs(dn)),
                            **market_record
                        }
                        recent_moves.append(move)
                        new_moves.append(move)

                    # Update last prices
                    last_prices[m["id"]] = (y, n)

                # Only include events with active markets
                if has_active_markets:
                    updated[eid] = {**meta, "markets": markets}

        with lock:
            # Update main data store - CRUCIAL for Render
            for eid, event_data in updated.items():
                events_data[eid] = event_data

            # Remove events that no longer have markets
            to_remove = [eid for eid in list(events_data.keys()) if eid not in updated]
            for eid in to_remove:
                events_data.pop(eid, None)
                events_meta.pop(eid, None)

            last_data_update = now

        # Log results
        logger.info(f"Processed {len(raw_events)} events, found {len(updated)} with markets")
        logger.info(f"Total events in data store: {len(events_data)}")
        scanner_status = f"Updated {len(updated)} events"

        return new_moves

    except Exception as e:
        logger.error(f"Error processing events: {e}", exc_info=True)
        scanner_status = f"Error: {str(e)[:100]}"


# ────────────────────────── BACKGROUND SCANNER ─────────────────────
def scan_loop():
    """Background scanner that continuously updates the data"""
    global scanner_status

    scanner_status = "Starting scanner"
    logger.info("Starting background scanner")

    # Initial data load
    try:
        scanner_status = "Initial scan"
        process_events()
    except Exception as e:
        logger.error(f"Error during initial scan: {e}", exc_info=True)
        scanner_status = f"Initial scan error: {str(e)[:100]}"

    # Continuous polling
    while True:
        try:
            # Wait for next scan
            time.sleep(SCAN_INTERVAL)

            # Process new data
            scanner_status = "Running scan"
            new_moves = process_events()

            # Handle sounds (only in local development)
            if new_moves and hasattr(playsound, '__call__'):
                top = max(new_moves, key=lambda m: max(m["yd"], m["nd"]))
                mag = max(top["yd"], top["nd"])
                try:
                    if mag > 5:
                        playsound("static/sound3.mp3")
                    elif mag > 1:
                        playsound("static/sound2.mp3")
                    elif mag > 0.3:
                        playsound("static/sound1.mp3")
                except Exception as e:
                    logger.warning(f"Failed to play sound: {e}")

        except Exception as e:
            logger.error(f"Error in scan loop: {e}", exc_info=True)
            scanner_status = f"Scan error: {str(e)[:100]}"
            time.sleep(5)  # Shorter wait on error


# ────────────────────────── ROUTES ─────────────────────────────────
@app.route('/static/<path:filename>')
def static_file(filename):
    return send_from_directory('static', filename)


@app.route('/debug')
def debug():
    """Debug endpoint to check application state"""
    with lock:
        return jsonify({
            'events_count': len(events_data),
            'recent_moves_count': len(recent_moves),
            'last_update': last_data_update,
            'seconds_since_update': time.time() - last_data_update if last_data_update else None,
            'scanner_status': scanner_status,
            'events_sample': list(events_data.keys())[:5]
        })


@app.route('/reset')
def reset():
    """Force a data reset and immediate scan"""
    global events_data, events_meta, last_prices, recent_moves, scanner_status

    with lock:
        events_data.clear()
        events_meta.clear()
        last_prices.clear()
        recent_moves.clear()

    # Force an immediate scan in a new thread
    threading.Thread(target=process_events, daemon=True).start()

    return jsonify({'status': 'reset_initiated'})


@app.route('/')
def index():
    """Main page showing all events"""
    global events_data

    now = time.time()
    events_snapshot = []

    with lock:
        # Create a deep copy of events to avoid thread issues
        for eid, event in events_data.items():
            # Create a new dict with a deep copy of markets
            event_copy = {
                'id': event.get('id', ''),
                'title': event.get('title', 'Untitled Event'),
                'link': event.get('link', '#'),
                'markets': list(event.get('markets', []))
            }
            events_snapshot.append(event_copy)

        # Get recent moves for sound effects
        new_moves = [mv for mv in recent_moves if now - mv["time_ts"] < SCAN_INTERVAL]

    # Sort events
    if request.args.get("sort") == "move":
        # Sort by move magnitude
        # First, identify events with recent moves
        latest_batch_time = now - SCAN_INTERVAL
        event_max_moves = {}

        for mv in recent_moves:
            if mv["time_ts"] >= latest_batch_time:
                eid = mv["event_id"]
                event_max_moves[eid] = max(event_max_moves.get(eid, 0), mv.get("max_move", 0))

        # Sort events: those with recent moves first, then by move size
        events_with_moves = [e for e in events_snapshot if e["id"] in event_max_moves]
        events_without_moves = [e for e in events_snapshot if e["id"] not in event_max_moves]

        events_with_moves.sort(key=lambda e: event_max_moves.get(e["id"], 0), reverse=True)
        events_snapshot = events_with_moves + events_without_moves
    else:
        # Default sort: by market update time, then by size
        last_move_times = {}
        max_move_at_time = {}

        for mv in recent_moves:
            eid = mv["event_id"]
            move_time = mv["time_ts"]
            move_size = mv.get("max_move", 0)

            if eid not in last_move_times or move_time > last_move_times[eid]:
                last_move_times[eid] = move_time
                max_move_at_time[eid] = move_size
            elif move_time == last_move_times[eid]:
                max_move_at_time[eid] = max(max_move_at_time.get(eid, 0), move_size)

        events_snapshot.sort(
            key=lambda e: (last_move_times.get(e["id"], 0), max_move_at_time.get(e["id"], 0)),
            reverse=True
        )

    # Render the template with our snapshot data
    return render_template_string(HOME_TEMPLATE,
                                  events=events_snapshot,
                                  new_moves=new_moves,
                                  events_count=len(events_snapshot),
                                  refresh_interval=REFRESH_INTERVAL,
                                  last_update=last_data_update,
                                  scanner_status=scanner_status
                                  )


@app.route("/recent")
def recent():
    """Page showing recent price movements"""
    now = time.time()
    cutoff = now - (HISTORY_MINUTES * 60)
    sort_by_move = request.args.get("sort") == "move"

    moves = []
    with lock:
        # Get recent moves
        moves = [m.copy() for m in recent_moves if m["time_ts"] >= cutoff]

    # Sort moves
    if sort_by_move:
        moves.sort(key=lambda m: m.get("max_move", 0), reverse=True)
    else:
        moves.sort(key=lambda m: m["time_ts"], reverse=True)

    return render_template_string(RECENT_TEMPLATE,
                                  moves=moves,
                                  refresh_interval=REFRESH_INTERVAL,
                                  caller_time=now,
                                  history_minutes=HISTORY_MINUTES,
                                  sort_by_move=sort_by_move,
                                  moves_count=len(moves),
                                  last_update=last_data_update,
                                  scanner_status=scanner_status
                                  )


# ────────────────────────── TEMPLATES ──────────────────────────────
# Include your templates here as strings

# CSS shared across pages
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
.status-bar {
    background: #333;
    padding: 5px 10px;
    margin-bottom: 10px;
    font-size: 0.9em;
    display: flex;
    justify-content: space-between;
}
"""

# Home page template
HOME_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Polymarket Live</title>
  <meta http-equiv="refresh" content="{{ refresh_interval }}">
  <style>
    body{background:#222;color:#ddd;font-family:sans-serif;padding:20px;margin:0;}
    a.btn,button.btn{display:inline-block;margin-right:10px;padding:6px 12px;
      background:#444;color:#ff69b4;text-decoration:none;border-radius:4px;cursor:pointer;}
    h1{margin-bottom:.5em}
    {{ BASE_STYLE }}
  </style>
</head>
<body>
  <h1>Polymarket Live Scanner</h1>

  <div class="status-bar">
    <div>Events: {{ events_count }} | Status: {{ scanner_status }}</div>
    <div>
      <a href="/debug" target="_blank">Debug</a> | 
      <a href="/reset">Reset Data</a>
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

  {% if events %}
    {% for ev in events %}
      <h2>{{ ev.title }}</h2>
      <a class="btn" href="{{ ev.link }}" target="_blank">View Event</a>
      <table>
        <thead><tr><th>Market (ID)</th><th>YES</th><th>NO</th><th>Δ YES</th><th>Δ NO</th></tr></thead>
        <tbody>
          {% for m in ev.markets %}
          <tr>
            <td class="market">{{ m.question }} ({{ m.id }})</td>
            <td class="yes">{{ '%.2f%%'|format(m.yes) }}</td>
            <td class="no">{{ '%.2f%%'|format(m.no) }}</td>
            <td class="{{ m.ydir|lower }}
                {% if m.yd>5 %}highlight-5{% elif m.yd>1 %}highlight-1{% elif m.yd>0.3 %}highlight-0-3{% endif %}">
                {{ m.ydir }} {{ '%.2f%%'|format(m.yd) }}
            </td>
            <td class="{{ m.ndir|lower }}
                {% if m.nd>5 %}highlight-5{% elif m.nd>1 %}highlight-1{% elif m.nd>0.3 %}highlight-0-3{% endif %}">
                {{ m.ndir }} {{ '%.2f%%'|format(m.nd) }}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endfor %}
  {% else %}
    <div style="text-align: center; margin-top: 50px; padding: 20px; background: #333;">
      <h3>No events with markets found</h3>
      <p>Status: {{ scanner_status }}</p>
      <p>Try <a href="/reset">resetting the data</a> or checking the <a href="/debug">debug information</a>.</p>
    </div>
  {% endif %}

  <script>
    /* countdown */
    let t={{ refresh_interval }};
    setInterval(()=>{t=t? t-1:{{ refresh_interval }};document.getElementById('timer').textContent=t;},1000);

    /* unlock sounds */
    const s0=new Audio('/static/sound1.mp3'),
          s1=new Audio('/static/sound2.mp3'),
          s5=new Audio('/static/sound3.mp3');
    document.getElementById('enable-sound').onclick=()=>{
      [s0,s1,s5].forEach(a=>{a.play().catch(()=>{});a.pause();a.currentTime=0;});
    };

    /* play one sound for newest batch (server provides new_moves) */
    const newMoves={{ new_moves|tojson }};
    if(newMoves.length){
      const top=newMoves.reduce((a,b)=>Math.max(b.yd,b.nd)>Math.max(a.yd,a.nd)?b:a, {yd:0, nd:0});
      const diff=Math.max(top.yd||0, top.nd||0);
      if(diff>5)      s5.play().catch(()=>{});
      else if(diff>1) s1.play().catch(()=>{});
      else if(diff>0.3) s0.play().catch(()=>{});
    }
  </script>
</body>
</html>
"""

# Recent moves template
RECENT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Recent Moves</title>
  <meta http-equiv="refresh" content="{{ refresh_interval }}">
  <style>
    body{background:#222;color:#ddd;font-family:sans-serif;padding:20px;margin:0;}
    a.btn,button.btn{display:inline-block;margin-right:10px;padding:6px 12px;
      background:#444;color:#ff69b4;text-decoration:none;border-radius:4px;cursor:pointer;}
    h1{margin-bottom:.5em}
    {{ BASE_STYLE }}
  </style>
</head>
<body>
  <h1>Recent Moves (last {{ history_minutes }} min)</h1>

  <div class="status-bar">
    <div>Moves: {{ moves_count }} | Status: {{ scanner_status }}</div>
    <div>
      <a href="/debug" target="_blank">Debug</a> | 
      <a href="/reset">Reset Data</a>
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

  {% if moves %}
    <table>
      <thead><tr>
        <th>Since</th><th>Event</th><th>Market (ID)</th>
        <th>YES</th><th>NO</th><th>Δ YES</th><th>Δ NO</th>
      </tr></thead>
      <tbody>
        {% set now = caller_time %}
        {% for m in moves %}
          {% set diff = now - m.time_ts %}
          {% set ago = diff<60 and (diff|round(0) ~ 's') or ((diff/60)|round(1) ~ 'm') %}
          <tr>
            <td>{{ ago }}</td>
            <td><a class="btn" href="{{ m.event_link }}" target="_blank">{{ m.event_title }}</a></td>
            <td class="market">{{ m.question }} ({{ m.market_id }})</td>
            <td class="yes">{{ '%.2f%%'|format(m.yes) }}</td>
            <td class="no">{{ '%.2f%%'|format(m.no) }}</td>
            <td class="{{ m.ydir|lower }}
                {% if m.yd>5 %}highlight-5{% elif m.yd>1 %}highlight-1{% elif m.yd>0.3 %}highlight-0-3{% endif %}">
                {{ m.ydir }} {{ '%.2f%%'|format(m.yd) }}
            </td>
            <td class="{{ m.ndir|lower }}
                {% if m.nd>5 %}highlight-5{% elif m.nd>1 %}highlight-1{% elif m.nd>0.3 %}highlight-0-3{% endif %}">
                {{ m.ndir }} {{ '%.2f%%'|format(m.nd) }}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div style="text-align: center; margin-top: 50px; padding: 20px; background: #333;">
      <h3>No recent price movements detected</h3>
      <p>Status: {{ scanner_status }}</p>
      <p>Price changes will appear here as they occur.</p>
    </div>
  {% endif %}

  <script>
    /* countdown */
    let t={{ refresh_interval }};
    setInterval(()=>{t=t? t-1:{{ refresh_interval }};document.getElementById('timer').textContent=t;},1000);

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


# ────────────────────────── INITIALIZATION ────────────────────────────

def init_app():
    """Initialize the application - called once at startup"""
    # Start background scanner thread
    threading.Thread(target=scan_loop, daemon=True).start()
    return app


# For compatibility with different deployment methods
app = init_app()

# For local development
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)