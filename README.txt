# Polymarket Terminal

A real-time monitoring application for Polymarket events and price movements.

## Project Structure

The project has been organized into a modular structure for better maintainability:

```
polymarket_terminal/
│
├── main.py                      # Main application entry point
├── requirements.txt             # Project dependencies
├── static/                      # Static files (sounds, etc.)
│   ├── sound1.mp3
│   ├── sound2.mp3
│   └── sound3.mp3
│
├── config.py                    # Configuration settings
├── api/
│   ├── __init__.py
│   └── polymarket_api.py        # API interaction functions
│
├── data/                        # Data storage directory
│   └── __init__.py              # Ensures directory is created
│
├── services/
│   ├── __init__.py
│   ├── scanner.py               # Background scanner service
│   ├── file_manager.py          # File operations
│   └── sound_manager.py         # Sound playback
│
└── templates/
    └── terminal.html            # Terminal HTML template
```

## Setup and Installation

1. Clone the repository
2. Create a virtual environment (optional but recommended)
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Make sure you have the sound files in the static directory:
   - sound1.mp3 (low magnitude change sound)
   - sound2.mp3 (medium magnitude change sound)
   - sound3.mp3 (high magnitude change sound)

## Running the Application

### Development Mode
```
python main.py
```

This will start the Flask server on http://localhost:5000

### Production Mode
For production deployment, use gunicorn:
```
gunicorn main:app -b 0.0.0.0:5000
```

## Components Overview

### Config (config.py)
Contains all configuration settings such as API URLs, refresh intervals, and file paths. Centralizing configuration makes it easy to modify settings.

### API Module (api/polymarket_api.py)
Handles all interaction with the Polymarket API, including fetching events, parsing prices, and creating event URLs.

### Services Module
- **scanner.py**: Contains the background scanner service that polls the Polymarket API for updates
- **file_manager.py**: Handles all file operations (reading/writing events, moves, status)
- **sound_manager.py**: Manages sound playback for price movements

### Templates
- **terminal.html**: HTML template for the terminal interface with embedded JavaScript for real-time updates

### Main Application (main.py)
Integrates all components and provides the Flask web interface for monitoring Polymarket events.

## Features

- Real-time monitoring of Polymarket events and price movements
- Sound alerts for significant price movements
- Filtering by minimum price movement percentage
- Persistent storage of events and price movements
- Debug interface for monitoring application status