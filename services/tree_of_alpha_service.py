import os
import sys
import json
import logging
import threading
import time
import websocket

# Add the project root directory to the Python path
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.append(current_dir)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
MAX_NEWS_ITEMS = 1000
API_KEY = "004932ff71009b9ed8fd6858dbd3fba5a4c9f6e500dafaf4566424c00f4117b7"

# File paths
NEWS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data/news_data.json")


class TreeOfAlphaNewsService:
    """Service to handle background scanning of Tree of Alpha news"""
    _stop_event = threading.Event()
    _thread = None
    _news_items = []
    _lock = threading.Lock()
    _ws = None
    _is_web_server_mode = False

    @classmethod
    def set_web_server_mode(cls, is_web_server_mode=True):
        """Set whether the service is running in web server mode"""
        cls._is_web_server_mode = is_web_server_mode
        logger.info(f"Set web server mode to: {is_web_server_mode}")

    @classmethod
    def is_valid_news_message(cls, data):
        """
        Validate if the received message is a news item
        """
        # First check if it's a login confirmation message (which should be ignored)
        if isinstance(data, dict) and data.get('type') == 'login':
            logger.info(f"Login response received: {data}")
            return False

        # Check if it's a ping/pong message
        if isinstance(data, str) and data.strip() == 'ping':
            logger.debug("Received ping message")
            return False

        # For dictionary data, check for news-specific fields
        if isinstance(data, dict):
            # Less strict validation - if it has a body or title, consider it news
            if 'body' in data or 'title' in data:
                return True

        logger.debug(f"Skipping non-news message: {data}")
        return False

    @classmethod
    def save_news_to_file(cls):
        """Save news items to file"""
        try:
            with cls._lock:
                # Ensure data directory exists
                os.makedirs(os.path.dirname(NEWS_FILE), exist_ok=True)

                with open(NEWS_FILE, 'w') as f:
                    json.dump(cls._news_items, f, indent=2)
                logger.info(f"Saved {len(cls._news_items)} news items to {NEWS_FILE}")
        except Exception as e:
            logger.error(f"Error saving news to file: {e}")

    @classmethod
    def load_news_from_file(cls):
        """Load news items from file"""
        try:
            if os.path.exists(NEWS_FILE):
                with open(NEWS_FILE, 'r') as f:
                    loaded_items = json.load(f)
                    with cls._lock:
                        cls._news_items = loaded_items
                logger.info(f"Loaded {len(cls._news_items)} news items from {NEWS_FILE}")
                return loaded_items
            else:
                logger.warning(f"News file not found: {NEWS_FILE}")
                return []
        except Exception as e:
            logger.error(f"Error loading news from file: {e}")
            return []

    @classmethod
    def on_message(cls, wsapp, message):
        """Handle incoming websocket messages"""
        try:
            # Check if it's a ping message and respond with pong
            if message == "ping":
                wsapp.send("pong")
                logger.debug("Responded to ping with pong")
                if not cls._is_web_server_mode:
                    print(".", end="", flush=True)  # Print a dot to indicate activity
                return

            # Log the raw message at debug level
            logger.debug(f"Raw message received: {message[:500]}...")

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.error(f"Error decoding JSON: {message[:100]}...")
                if not cls._is_web_server_mode:
                    print(f"\n⚠️ Received non-JSON message: {message[:50]}...")
                return

            # Validate and process only news messages
            if not cls.is_valid_news_message(data):
                # If it's a login response, print a confirmation
                if isinstance(data, dict) and data.get('type') == 'login':
                    logger.info("Login successful!")
                    if not cls._is_web_server_mode:
                        print("\n✅ Successfully logged in to Tree of Alpha service!")
                        sys.stdout.flush()
                return

            # Ensure the message has a unique ID and timestamp
            if '_id' not in data:
                data['_id'] = f"news_{int(time.time() * 1000)}"

            if 'time' not in data:
                data['time'] = int(time.time() * 1000)

            with cls._lock:
                # Check if news item already exists to avoid duplicates
                if any(item.get('_id') == data.get('_id') for item in cls._news_items):
                    logger.debug(f"Skipping duplicate news item: {data.get('_id')}")
                    return

                # Add to news items
                cls._news_items.append(data)

                # Keep only the last MAX_NEWS_ITEMS
                if len(cls._news_items) > MAX_NEWS_ITEMS:
                    cls._news_items = cls._news_items[-MAX_NEWS_ITEMS:]

                # Save periodically or when we hit certain thresholds
                cls.save_news_to_file()

                # Log each news item
                logger.info(f"Processed news: {data.get('title', 'No Title')}")

                # Only print to console in standalone mode
                if not cls._is_web_server_mode:
                    # Print news to console with high visibility
                    news_output = "\n" + "!" * 50
                    news_output += "\n!!! NEW CRYPTOCURRENCY NEWS !!!"
                    news_output += "\n" + "!" * 50
                    news_output += f"\n✦ Title: {data.get('title', 'No Title')}"
                    news_output += f"\n✦ Source: {data.get('source', 'Unknown')}"
                    news_output += f"\n✦ Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('time', int(time.time() * 1000)) / 1000))}"
                    news_output += f"\n✦ Body: {data.get('body', 'No Content')[:150]}..."
                    if 'suggestions' in data and data['suggestions']:
                        coins = ', '.join([s.get('coin', '') for s in data['suggestions'] if 'coin' in s])
                        news_output += f"\n✦ Coins: {coins}"
                    news_output += "\n" + "!" * 50 + "\n"

                    # Use multiple methods to ensure visibility
                    print(news_output)
                    sys.stdout.flush()  # Force flush to terminal

                # Always log at ERROR level for higher visibility in logs
                logger.error(f"NEWS ALERT: {data.get('title', 'No Title')}")

        except json.JSONDecodeError:
            logger.error(f"Error decoding message: {message}")
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            if not cls._is_web_server_mode:
                print(f"\n❌ Error processing message: {str(e)}")
                sys.stdout.flush()

    @classmethod
    def on_error(cls, wsapp, error):
        """Handle websocket errors"""
        logger.error(f"WebSocket Error: {str(error)}")
        if not cls._is_web_server_mode:
            print(f"\n❌ WebSocket Error: {str(error)}")
            sys.stdout.flush()

    @classmethod
    def on_close(cls, wsapp, close_status_code, close_msg):
        """Handle websocket close event"""
        logger.warning(f"WebSocket connection closed. Status: {close_status_code}, Message: {close_msg}")
        if not cls._is_web_server_mode:
            print(f"\n🔌 WebSocket connection closed. Status: {close_status_code}, Message: {close_msg}")
            sys.stdout.flush()

    @classmethod
    def on_open(cls, wsapp):
        """Handle websocket open event"""
        logger.info("WebSocket connection established")
        try:
            wsapp.send(f"login {API_KEY}")
            logger.info("Sent login with API key")
            if not cls._is_web_server_mode:
                print("\n🔑 Sent login request with API key")
                sys.stdout.flush()
        except Exception as e:
            logger.error(f"Error sending login: {e}")
            if not cls._is_web_server_mode:
                print(f"\n❌ Error sending login: {e}")
                sys.stdout.flush()

    @classmethod
    def connect_websocket(cls):
        """Connect to the Tree of Alpha websocket"""
        logger.info("Starting Tree of Alpha WebSocket connection")

        # Load existing news items
        cls.load_news_from_file()
        logger.info(f"Existing news items loaded: {len(cls._news_items)}")

        # Print confirmation to standard output (only in standalone mode)
        if not cls._is_web_server_mode:
            print(f"\n🔄 Loaded {len(cls._news_items)} existing news items from storage")
            print(f"📶 Establishing WebSocket connection to Tree of Alpha...")
            sys.stdout.flush()

        reconnect_count = 0
        while not cls._stop_event.is_set():
            try:
                # Only enable trace in standalone mode to avoid polluting web server logs
                if not cls._is_web_server_mode:
                    websocket.enableTrace(True)
                else:
                    websocket.enableTrace(False)

                # Create websocket connection with ping interval
                cls._ws = websocket.WebSocketApp(
                    "wss://news.treeofalpha.com/ws",
                    on_open=cls.on_open,
                    on_message=cls.on_message,
                    on_error=cls.on_error,
                    on_close=cls.on_close
                )

                # Print connection status (only in standalone mode)
                if not cls._is_web_server_mode:
                    print(f"🔌 Attempting connection to wss://news.treeofalpha.com/ws...")
                    sys.stdout.flush()

                # Run with automatic reconnection and ping interval
                cls._ws.run_forever(ping_interval=30, ping_timeout=5, reconnect=5)

                if cls._stop_event.is_set():
                    break

                reconnect_count += 1
                logger.warning(f"Connection lost. Attempting to reconnect... (Attempt {reconnect_count})")
                if not cls._is_web_server_mode:
                    print(f"\n⚠️ Connection lost. Attempting to reconnect... (Attempt {reconnect_count})")
                    sys.stdout.flush()
                time.sleep(5)

            except Exception as e:
                reconnect_count += 1
                logger.error(f"Error in websocket connection: {str(e)}")
                if not cls._is_web_server_mode:
                    print(f"\n❌ Error in websocket connection: {str(e)}")
                    print(f"⏱️ Retrying in 5 seconds... (Attempt {reconnect_count})")
                    sys.stdout.flush()
                time.sleep(5)

    @classmethod
    def start_scanner(cls, web_server_mode=False):
        """Start the news scanner in a background thread"""
        # Set the mode based on how we're being called
        cls.set_web_server_mode(web_server_mode)

        if cls._thread and cls._thread.is_alive():
            logger.info("Scanner already running")
            return

        # Reset stop event
        cls._stop_event.clear()

        # Start new thread
        cls._thread = threading.Thread(target=cls.connect_websocket, daemon=True)
        cls._thread.start()
        logger.info("News scanner started in background thread")

        if not cls._is_web_server_mode:
            # Clear the screen in standalone mode
            os.system('cls' if os.name == 'nt' else 'clear')
            print("\n" + "*" * 70)
            print("*" * 5 + " TREE OF ALPHA CRYPTOCURRENCY NEWS SERVICE STARTED " + "*" * 5)
            print("*" * 70)
            print("\n⏳ Waiting for news to arrive...")
            print("📊 News will be displayed in real-time")
            print("🛑 Press Ctrl+C to exit\n")
            sys.stdout.flush()

    @classmethod
    def stop_scanner(cls):
        """Stop the scanner thread"""
        cls._stop_event.set()
        if cls._ws:
            cls._ws.close()
        if cls._thread:
            cls._thread.join(timeout=10)
        logger.info("News scanner stopped")
        if not cls._is_web_server_mode:
            print("\n👋 News scanner stopped")
            sys.stdout.flush()

    @classmethod
    def get_news_data(cls):
        """Get current news data (thread-safe)"""
        with cls._lock:
            return cls._news_items.copy()

    @classmethod
    def add_test_news(cls):
        """Add a test news item (for debugging)"""
        test_news = {
            "_id": "test_" + str(int(time.time())),
            "time": int(time.time() * 1000),
            "title": "🔍 TEST CRYPTO ALERT 🔍",
            "body": "Bitcoin just broke $100,000! This is a test news item added for debugging purposes. In a real scenario, you would see important cryptocurrency news here.",
            "source": "Test",
            "suggestions": [{"coin": "BTC"}, {"coin": "ETH"}, {"coin": "SOL"}]
        }

        with cls._lock:
            cls._news_items.append(test_news)
            cls.save_news_to_file()

        logger.info("Added test news item")

        # Only print to console in standalone mode
        if not cls._is_web_server_mode:
            # Print the test news to console with high visibility
            test_output = "\n" + "#" * 70
            test_output += "\n########## 🚨 TEST NEWS ITEM ADDED 🚨 ##########"
            test_output += "\n" + "#" * 70
            test_output += f"\n✦ Title: {test_news['title']}"
            test_output += f"\n✦ Body: {test_news['body']}"
            test_output += f"\n✦ Coins: BTC, ETH, SOL"
            test_output += "\n" + "#" * 70 + "\n"

            print(test_output)
            sys.stdout.flush()  # Force flush to terminal

    @classmethod
    def display_recent_news(cls, count=5):
        """Display the most recent news items"""
        with cls._lock:
            items = cls._news_items[-count:] if len(cls._news_items) >= count else cls._news_items

        if not items:
            if not cls._is_web_server_mode:
                print("\n" + "=" * 60)
                print("⚠️ NO NEWS ITEMS AVAILABLE ⚠️")
                print("=" * 60 + "\n")
            return

        # Only print to console in standalone mode
        if not cls._is_web_server_mode:
            header = "\n" + "=" * 60
            header += f"\n====== DISPLAYING {len(items)} RECENT NEWS ITEMS ======"
            header += "\n" + "=" * 60
            print(header)

            for idx, item in enumerate(reversed(items), 1):
                print(f"\n{'#' * 50}")
                print(f"📰 NEWS ITEM {idx} 📰")
                print(f"✦ Title: {item.get('title', 'No Title')}")
                print(f"✦ Source: {item.get('source', 'Unknown')}")
                print(f"✦ Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item.get('time', 0) / 1000))}")
                print(f"✦ Body: {item.get('body', 'No Content')[:200]}...")
                if 'suggestions' in item and item['suggestions']:
                    coins = ', '.join([s.get('coin', '') for s in item['suggestions'] if 'coin' in s])
                    print(f"✦ Coins: {coins}")
                print(f"{'#' * 50}")

            footer = "\n" + "=" * 60
            footer += "\n============== END OF NEWS ITEMS ==============="
            footer += "\n" + "=" * 60 + "\n"
            print(footer)
            sys.stdout.flush()  # Force flush to ensure output is displayed


# Example usage when run directly
if __name__ == "__main__":
    try:
        # Start the news scanner in standalone mode (not web server mode)
        TreeOfAlphaNewsService.start_scanner(web_server_mode=False)

        # Keep the script running and display news periodically
        count = 0
        while True:
            time.sleep(10)
            count += 1

            # Check the current count of news items
            news_count = len(TreeOfAlphaNewsService.get_news_data())
            current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            status_msg = f"[{current_time}] 📡 Connection active. News items: {news_count}"
            print(status_msg)
            sys.stdout.flush()

            # Display recent news every 60 seconds
            if count % 6 == 0:
                TreeOfAlphaNewsService.display_recent_news(5)

            # If no news after 2 minutes, add a test news item
            if count == 12 and news_count == 0:
                print("\n⚠️ No news received after 2 minutes. Adding test news item for verification...")
                sys.stdout.flush()
                TreeOfAlphaNewsService.add_test_news()

    except KeyboardInterrupt:
        print("\n\n" + "-" * 70)
        print("👋 Shutting down Tree of Alpha News Service...")
        TreeOfAlphaNewsService.stop_scanner()
        TreeOfAlphaNewsService.save_news_to_file()
        print("✅ Service stopped and data saved.")
        print("-" * 70 + "\n")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"\n❌ ERROR: {str(e)}")
        TreeOfAlphaNewsService.stop_scanner()