import os
import time
import threading
import json
import requests
import logging
from collections import deque
from flask import Flask, render_template_string, request, send_from_directory
from slugify import slugify

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

# ────────────────────────── APP & STATE ────────────────────────────
app = Flask(__name__)
lock = threading.RLock()  # For thread safety

# Use more efficient data structures
events_meta = {}  # id -> {id,title,link}
events_data = {}  # id -> {id,title,link,markets:[…]}
last_prices = {}  # market_id -> (yes,no)
recent_moves = deque(maxlen=MAX_MOVES)  # Fixed-size queue

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
"""


# ────────────────────────── API HELPERS ────────────────────────────
def fetch_all_events(params=None, page_size=100, max_retries=3):
    params = params or {}
    out, offset = [], 0

    try:
        logger.info("Starting to fetch events from API")

        while True:
            q = params.copy()
            q.update(limit=page_size, offset=offset)

            for attempt in range(max_retries):
                try:
                    logger.debug(f"Fetching events batch with offset={offset}")
                    r = requests.get(f"{API_BASE}/events", params=q, timeout=30)
                    r.raise_for_status()
                    batch = r.json()

                    if not batch:
                        logger.debug("Empty batch received, ending pagination")
                        break

                    logger.debug(f"Received {len(batch)} events in this batch")
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

        logger.info(f"Successfully fetched {len(out)} events total")
        return out

    except Exception as e:
        logger.error(f"Error in fetch_all_events: {e}", exc_info=True)
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
        # On Render.com, we might not be able to play sounds, so just log
        logger.info(f"Would play sound: {path}")
    except Exception as e:
        logger.warning(f"Failed to play sound: {e}")


# ────────────────────────── BACKGROUND SCANNER ─────────────────────
def scan_loop():
    global events_meta, events_data, last_prices, recent_moves

    logger.info("🔁 Scan loop started")

    params = {"closed": False, "archived": False, "active": True}

    # initial seed
    try:
        initial_events = fetch_all_events(params)
        logger.info(f"Initial scan retrieved {len(initial_events)} events")

        for ev in initial_events:
            eid = ev["id"]
            with lock:
                events_meta[eid] = {"id": eid, "title": ev["title"], "link": make_event_url(ev)}

                # Create market entries for this event
                mkts = []
                for m in ev.get("markets", []):
                    p = parse_prices(m.get("outcomePrices"))
                    if p:
                        y, n = p
                        last_prices[m["id"]] = (y, n)

                        # Add market to this event's markets list
                        mkts.append({
                            "id": m["id"],
                            "question": m["question"],
                            "yes": y * 100,
                            "no": n * 100,
                            "yd": 0,
                            "nd": 0,
                            "ydir": "—",
                            "ndir": "—",
                            "max_move": 0
                        })

                # Only add event to events_data if it has markets
                if mkts:
                    events_data[eid] = {**events_meta[eid], "markets": mkts}

        logger.info(f"Initial scan completed, found {len(events_data)} events with markets")
    except Exception as e:
        logger.error(f"Error during initial scan: {e}", exc_info=True)

    # Main loop for continuous scanning
    while True:
        time.sleep(SCAN_INTERVAL)
        now = time.time()
        new_moves = []

        try:
            current_events = fetch_all_events(params)
            logger.info(f"Fetched {len(current_events)} events in this scan cycle")

            # Track which events we've updated this cycle
            updated_events = {}

            for ev in current_events:
                eid = ev["id"]

                # Update metadata
                with lock:
                    meta = events_meta.setdefault(
                        eid,
                        {"id": eid, "title": ev["title"], "link": make_event_url(ev)}
                    )
                    meta["title"] = ev["title"]

                    # Process markets for this event
                    mkts = []
                    for m in ev.get("markets", []):
                        p = parse_prices(m.get("outcomePrices", []))
                        if not p:
                            continue

                        y, n = p
                        py, pn = last_prices.get(m["id"], (y, n))
                        dy, dn = (y - py) * 100, (n - pn) * 100

                        rec = {
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
                        mkts.append(rec)

                        # Record significant price moves
                        if dy != 0 or dn != 0:
                            move = {
                                "time_ts": now,
                                "event_id": eid,
                                "event_title": ev["title"],
                                "event_link": make_event_url(ev),
                                "market_id": m["id"],
                                "question": m["question"],
                                "max_move": max(abs(dy), abs(dn)),
                                **rec
                            }
                            recent_moves.append(move)
                            new_moves.append(move)

                        # Update last known prices
                        last_prices[m["id"]] = (y, n)

                    # Only add events with markets to our updated dictionary
                    if mkts:
                        updated_events[eid] = {**meta, "markets": mkts}

            # Update the global events_data dictionary
            with lock:
                # CRITICAL FIX: Don't replace the entire dictionary if we didn't get data
                if updated_events:
                    # Update events_data with new data
                    events_data.update(updated_events)

                    # Remove events that no longer have markets
                    for eid in list(events_data.keys()):
                        if eid not in updated_events:
                            events_data.pop(eid, None)

                    logger.info(f"Updated events_data with {len(updated_events)} events")
                else:
                    logger.warning("No events with markets found in this scan cycle")

            # Handle sound notifications for price movements
            if new_moves:
                top = max(new_moves, key=lambda m: max(m["yd"], m["nd"]))
                mag = max(top["yd"], top["nd"])
                if mag > 5:
                    play_sound_async("static/sound3.mp3")
                elif mag > 1:
                    play_sound_async("static/sound2.mp3")
                elif mag > 0.3:
                    play_sound_async("static/sound1.mp3")

            logger.info(f"Scanned {len(current_events)} events, found {len(new_moves)} moves")
            logger.info(f"Currently tracking {len(events_data)} events with markets")

        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)


def start_scanner():
    threading.Thread(target=scan_loop, daemon=True).start()


# ────────────────────────── TEMPLATES ──────────────────────────────
HOME_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Polymarket Live</title>
  <meta http-equiv="refresh" content="{{ refresh_interval }}">
  <link rel="manifest" href="/manifest.json">
  <script>
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/service-worker.js')
          .then(reg => console.log('Service Worker registered', reg))
          .catch(err => console.log('Service Worker registration failed', err));
      });
    }
  </script>
  <style>
    body{background:#222;color:#ddd;font-family:sans-serif;padding:20px}
    a.btn,button.btn{display:inline-block;margin-right:10px;padding:6px 12px;
      background:#444;color:#ff69b4;text-decoration:none;border-radius:4px;cursor:pointer;}
    h1{margin-bottom:.5em}
    {{ BASE_STYLE }}
  </style>
</head>
<body>
  <h1>Polymarket Live Scanner</h1>
  <div class="btn-container">
    <div>
      <button id="enable-sound" class="btn">Enable Sounds</button>
      <a class="btn" href="/recent">Recent Moves</a>
    </div>
    <div class="right-align">
      <button onclick="location='/?sort=move';" class="btn">Sort by Biggest Move</button>
    </div>
  </div>
  <p>Next refresh in <span id="timer">--</span>s | Events: {{ events|length }}</p>

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
    <div style="text-align: center; margin-top: 50px;">
      <h3>No events with markets found</h3>
      <p>This may be temporary while the system loads data. The page will refresh automatically.</p>
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
      const diff=Math.max(top.yd,top.nd);
      if(diff>5)      s5.play();
      else if(diff>1) s1.play();
      else if(diff>0.3) s0.play();
    }
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
</head>
<body>
  <h1>Recent Moves (last {{ history_minutes }} min)</h1>
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
  <p>Next refresh in <span id="timer">--</span>s | Moves: {{ moves|length }}</p>

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
    <div style="text-align: center; margin-top: 50px;">
      <h3>No recent price movements detected</h3>
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


# ────────────────────────── ROUTES ─────────────────────────────────
@app.route('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')


@app.route('/service-worker.js')
def service_worker():
    return app.send_static_file('service-worker.js')


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)


@app.route("/debug-state")
def debug_state():
    """Debug endpoint to check the state of the application"""
    with lock:
        return {
            "events_data_count": len(events_data),
            "events_meta_count": len(events_meta),
            "recent_moves_count": len(recent_moves),
            "event_sample": list(events_data.keys())[:5] if events_data else []
        }


@app.route("/")
def index():
    """Home page showing all active events with direct data access"""
    now = time.time()

    # Get direct access to the data
    events_snapshot = []

    with lock:
        # Get a direct copy of the events
        events_snapshot = list(events_data.values())
        logger.info(f"📦 Sending {len(events_snapshot)} events to frontend")

        if request.args.get("sort") == "move":
            # Implement the logic for sorting by move magnitude
            latest_batch_time = max([mv["time_ts"] for mv in recent_moves], default=now) if recent_moves else now
            batch_cutoff = latest_batch_time - SCAN_INTERVAL

            # Identify which events had moves in the latest batch
            latest_batch_events = set()
            latest_batch_event_moves = {}  # event_id -> max_move in latest batch

            for mv in recent_moves:
                if mv["time_ts"] >= batch_cutoff:
                    eid = mv["event_id"]
                    latest_batch_events.add(eid)
                    latest_batch_event_moves[eid] = max(
                        latest_batch_event_moves.get(eid, 0),
                        mv.get("max_move", 0)
                    )

            # Separate events into two groups
            latest_evs = [e for e in events_snapshot if e["id"] in latest_batch_events]
            other_evs = [e for e in events_snapshot if e["id"] not in latest_batch_events]

            # Sort latest events by magnitude of their moves
            latest_evs.sort(
                key=lambda e: latest_batch_event_moves.get(e["id"], 0),
                reverse=True
            )

            # Combine lists: latest batch events first (sorted by move size), then others
            events_snapshot = latest_evs + other_evs
        else:
            # Sort by time -> move size
            # First, get the latest move time and move size for each event
            event_latest_time = {}
            event_max_move_at_time = {}

            for mv in recent_moves:
                eid = mv["event_id"]
                move_time = mv["time_ts"]
                move_size = mv.get("max_move", 0)

                if eid not in event_latest_time or move_time > event_latest_time[eid]:
                    # This is a newer move for this event
                    event_latest_time[eid] = move_time
                    event_max_move_at_time[eid] = move_size
                elif move_time == event_latest_time[eid]:
                    # This is from the same timestamp, keep track of the largest move
                    event_max_move_at_time[eid] = max(event_max_move_at_time[eid], move_size)

            # Sort events by: 1) latest move time, 2) move magnitude at that time
            events_snapshot.sort(
                key=lambda e: (
                    event_latest_time.get(e["id"], 0),  # Sort by time (highest/newest first)
                    event_max_move_at_time.get(e["id"], 0)  # Then by move size
                ),
                reverse=True  # Descending order (newest and biggest first)
            )

        # Get new moves for sound effects
        new_moves = [mv for mv in recent_moves if now - mv["time_ts"] < SCAN_INTERVAL]

    return render_template_string(
        HOME_TEMPLATE,
        events=events_snapshot,
        new_moves=new_moves,
        BASE_STYLE=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL
    )


@app.route("/recent")
def recent():
    now = time.time()
    cutoff = now - (HISTORY_MINUTES * 60)
    sort_by_move = request.args.get("sort") == "move"

    with lock:
        # Filter recent moves efficiently
        moves = [mv for mv in recent_moves if mv["time_ts"] >= cutoff]
        logger.info(f"📦 Sending {len(moves)} recent moves to frontend")

        # Sort based on user preference
        if sort_by_move:
            moves.sort(key=lambda mv: mv.get("max_move", 0), reverse=True)
        else:
            moves.sort(key=lambda mv: mv["time_ts"], reverse=True)

    return render_template_string(
        RECENT_TEMPLATE,
        moves=moves,
        BASE_STYLE=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL,
        caller_time=now,
        history_minutes=HISTORY_MINUTES,
        sort_by_move=sort_by_move
    )


# ────────────────────────── STARTUP ────────────────────────────────
# Start the scanner thread - this must be outside the __main__ check for Render
start_scanner()

# For local development only - Render.com handles this part automatically
if __name__ == "__main__":
    app.run(debug=False)