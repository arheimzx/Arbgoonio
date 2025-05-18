import requests
import logging
import json
from slugify import slugify
from config import API_BASE

# Configure logging
logger = logging.getLogger(__name__)


def fetch_all_events(params=None, page_size=100, max_retries=3):
    """Fetch all events from the Polymarket API with pagination support"""
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
                import time
                time.sleep(2)  # Wait before retry

        if not batch or len(batch) < page_size:
            break

    return out


def make_event_url(ev):
    """Create a user-friendly URL for the event"""
    tid = ev["id"]
    slug = ev.get("slug") or slugify(ev["title"])
    return f"https://polymarket.com/event/{slug}?tid={tid}"


def parse_prices(raw):
    """Parse price data from API response"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None

    try:
        return float(raw[0]), float(raw[1])
    except (IndexError, TypeError, ValueError):
        return None