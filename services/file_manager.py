import os
import json
import logging
from config import DATA_DIR, EVENTS_FILE, MOVES_FILE, STATUS_FILE

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


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
            return data
        return []
    except Exception as e:
        logger.error(f"Error loading moves from file: {e}")
        return []


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