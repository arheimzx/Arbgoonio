import os
import time
import threading
import json
import requests
import logging
import playsound
from collections import deque
from flask import Flask, render_template_string, request, jsonify

# ────────────────────────── CONFIGURATION ──────────────────────────
API_BASE = "https://gamma-api.polymarket.com"
SCAN_INTERVAL = 15  # seconds between API polls (increased for Render)
REFRESH_INTERVAL = 5  # seconds between browser refreshes
HISTORY_MINUTES = 5  # minutes to keep price movement history
MAX_MOVES = 500  # maximum number of moves to store

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ────────────────────────── APP SETUP ──────────────────────────────
app = Flask(__name__)

# Global state with lock
events_data = {}
recent_moves = deque(maxlen=MAX_MOVES)
lock = threading.RLock()
last_update = 0
last_fetch_status = "Not started"

# ────────────────────────── CSS STYLES ──────────────────────────────
BASE_STYLE = """
body { background: #222; color: #ddd; font-family: sans-serif; padding: 20px; }
a, button { color: #ff69b4; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px; border-bottom: 1px solid #555; text-align: left; }
.yes { color: #0f0; font-weight: bold; }
.no { color: #f00; font-weight: bold; }
.up { color: #0f0; }
.down { color: #f00; }
.status { background: #333; padding: 10px; margin-bottom: 15px; }
"""


# ────────────────────────── FETCH FUNCTION ──────────────────────────
def fetch_data():
    """Fetch data from API and update global state"""
    global events_data, recent_moves, last_update, last_fetch_status

    try:
        last_fetch_status = "Starting fetch..."
        logger.info("Starting API fetch")

        # Make a simple API request with fewer parameters to debug
        params = {"limit": 100, "active": True}
        response = requests.get(f"{API_BASE}/events", params=params, timeout=30)

        if not response.ok:
            logger.error(f"API request failed: {response.status_code} {response.reason}")
            last_fetch_status = f"API error: {response.status_code} {response.reason}"
            return

        events_list = response.json()
        logger.info(f"Fetched {len(events_list)} events")
        last_fetch_status = f"Fetched {len(events_list)} events successfully"

        # If no events, don't process further
        if not events_list:
            logger.warning("No events returned from API")
            return

        # Process events
        processed_events = {}
        now = time.time()

        for event in events_list:
            event_id = event.get("id")
            if not event_id:
                continue

            # Create simplified event data
            title = event.get("title", "Untitled")
            markets = []

            # Process markets if present
            for market in event.get("markets", []):
                market_id = market.get("id")
                question = market.get("question", "Unknown")

                # Process prices
                prices = market.get("outcomePrices")
                if not prices or len(prices) < 2:
                    continue

                try:
                    yes_price = float(prices[0]) * 100  # Convert to percentage
                    no_price = float(prices[1]) * 100

                    # Add market to list
                    markets.append({
                        "id": market_id,
                        "question": question,
                        "yes": yes_price,
                        "no": no_price,
                        "yd": 0,  # Placeholder for now
                        "nd": 0,
                        "ydir": "—",
                        "ndir": "—"
                    })
                except (ValueError, TypeError, IndexError):
                    continue

            # Only include events with markets
            if markets:
                processed_events[event_id] = {
                    "id": event_id,
                    "title": title,
                    "link": f"https://polymarket.com/event/{event_id}",
                    "markets": markets
                }

        # Update global state
        with lock:
            events_data = processed_events
            last_update = now

        logger.info(f"Updated {len(processed_events)} events")
        last_fetch_status = f"Updated {len(processed_events)} events"

    except requests.exceptions.RequestException as e:
        logger.error(f"Request exception: {e}")
        last_fetch_status = f"Request error: {str(e)[:100]}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        last_fetch_status = f"Error: {str(e)[:100]}"


# ────────────────────────── BACKGROUND WORKER ──────────────────────────
def background_worker():
    """Background thread to fetch data periodically"""
    logger.info("Starting background worker")

    # Initial fetch
    fetch_data()

    # Continuous fetching
    while True:
        try:
            time.sleep(SCAN_INTERVAL)
            fetch_data()
        except Exception as e:
            logger.error(f"Error in background worker: {e}")
            time.sleep(5)  # Wait a bit on error


# ────────────────────────── ROUTES ──────────────────────────────
@app.route('/debug')
def debug():
    """Debug endpoint to check application state"""
    return jsonify({
        'events_count': len(events_data),
        'recent_moves_count': len(recent_moves),
        'last_update': last_update,
        'seconds_since_update': time.time() - last_update if last_update else None,
        'fetch_status': last_fetch_status,
        'event_ids': list(events_data.keys())[:10]
    })


@app.route('/fetch')
def manual_fetch():
    """Manually trigger a fetch operation"""
    threading.Thread(target=fetch_data, daemon=True).start()
    return jsonify({'status': 'fetch_initiated'})


@app.route('/')
def index():
    """Main page showing all events"""
    # Get snapshot of current data
    current_events = []
    with lock:
        for event in events_data.values():
            current_events.append(event)

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Polymarket Scanner</title>
        <meta http-equiv="refresh" content="{{ refresh }}">
        <style>{{ style }}</style>
    </head>
    <body>
        <h1>Polymarket Scanner</h1>

        <div class="status">
            <p>Status: {{ status }}</p>
            <p>Events: {{ events|length }} | Last update: {{ time_since }} seconds ago</p>
            <p>
                <a href="/debug" target="_blank">View Debug</a> | 
                <a href="/fetch">Force Update</a> | 
                <a href="/">Refresh</a>
            </p>
        </div>

        {% if events %}
            {% for event in events %}
                <h2>{{ event.title }}</h2>
                <a href="{{ event.link }}" target="_blank">View on Polymarket</a>

                <table>
                    <thead>
                        <tr>
                            <th>Market</th>
                            <th>YES</th>
                            <th>NO</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for market in event.markets %}
                            <tr>
                                <td>{{ market.question }}</td>
                                <td class="yes">{{ "%.2f%%"|format(market.yes) }}</td>
                                <td class="no">{{ "%.2f%%"|format(market.no) }}</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% endfor %}
        {% else %}
            <div style="padding: 20px; background: #333; margin-top: 20px; text-align: center;">
                <h3>No events available</h3>
                <p>{{ status }}</p>
                <p>Try clicking "Force Update" above.</p>
            </div>
        {% endif %}
    </body>
    </html>
    """,
                                  events=current_events,
                                  style=BASE_STYLE,
                                  refresh=REFRESH_INTERVAL,
                                  status=last_fetch_status,
                                  time_since=int(time.time() - last_update) if last_update else "never"
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


# ────────────────────────── APP INITIALIZATION ──────────────────────────
def init_app():
    """Initialize the application and start background worker"""
    # Start background worker in a daemon thread
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    return app

# For compatibility with different WSGI servers
application = init_app()

# For local development
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)