import os
import logging
from flask import Flask, render_template, request, send_file, json
from config import BASE_STYLE, REFRESH_INTERVAL, HISTORY_MINUTES
from services.scanner import start_scanner
from services.file_manager import load_status
from config import EVENTS_FILE, MOVES_FILE, STATUS_FILE, DATA_DIR

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)


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
    # Import here to avoid circular imports
    from services.scanner import scan_loop
    import threading
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
    """Terminal page is now the main page"""
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"User access from IP: {user_ip}")

    return render_template(
        'terminal.html',
        base_style=BASE_STYLE,
        refresh_interval=REFRESH_INTERVAL,
        history_minutes=HISTORY_MINUTES
    )


def main():
    """Main entry point"""
    # Ensure data directory exists
    os.makedirs(DATA_DIR, exist_ok=True)

    # Start the scanner thread
    start_scanner()

    # Run the Flask app
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()