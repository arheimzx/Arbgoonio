import threading
import logging
from playsound import playsound

# Configure logging
logger = logging.getLogger(__name__)


def play_sound_async(path):
    """Play a sound file asynchronously"""
    try:
        threading.Thread(target=lambda: playsound(path), daemon=True).start()
    except Exception as e:
        logger.warning(f"Failed to play sound: {e}")