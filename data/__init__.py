# Data directory initialization
import os
from config import DATA_DIR

# Ensure the data directory exists
os.makedirs(DATA_DIR, exist_ok=True)