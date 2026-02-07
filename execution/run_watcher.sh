#!/bin/bash
# Wrapper script for LaunchAgent to run the iCloud watcher

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( dirname "$SCRIPT_DIR" )"

# Activate virtual environment if it exists
if [ -d "$PROJECT_DIR/venv" ]; then
    source "$PROJECT_DIR/venv/bin/activate"
fi

# Set PYTHONUNBUFFERED for immediate logging
export PYTHONUNBUFFERED=1

# Load environment variables
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Add project bin to PATH for ffmpeg/ffprobe
export PATH="$PROJECT_DIR/bin:$PATH"

# Run the watcher
cd "$SCRIPT_DIR"
exec python3 watch_icloud.py
