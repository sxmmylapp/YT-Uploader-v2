#!/usr/bin/env python3
"""
iCloud Video Queue Watcher

Monitors ~/Library/Mobile Documents/com~apple~CloudDocs/VideoQueue_v2/ for new videos
and uploads them to the Railway server with Telegram preview notifications.
"""

import os
import sys
import time
import shutil
import hashlib
import subprocess
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from logging_config import setup_watcher_logging, log_exception

# Load environment variables
load_dotenv()

# Configuration
RAILWAY_URL = os.getenv("RAILWAY_URL", "").rstrip("/")
VIDEO_QUEUE_PATH = os.path.expanduser(
    os.getenv("VIDEO_QUEUE_PATH", "~/Library/Mobile Documents/com~apple~CloudDocs/VideoQueue_v2")
)
LOCAL_ARCHIVE_PATH = os.path.expanduser("~/Local Documents/YT Video Archive")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
POLL_INTERVAL = 3  # seconds
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}

# Chunk sizes based on file size
CHUNK_SIZE_SMALL = 15 * 1024 * 1024   # 15MB for files < 100MB
CHUNK_SIZE_MEDIUM = 25 * 1024 * 1024  # 25MB for files 100MB-1GB
CHUNK_SIZE_LARGE = 50 * 1024 * 1024   # 50MB for files > 1GB

# Initialize logging
logger, history = setup_watcher_logging()


def create_session():
    """Create a requests session with retry strategy."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_chunk_size(file_size: int) -> int:
    """Return appropriate chunk size based on file size."""
    if file_size < 100 * 1024 * 1024:  # < 100MB
        return CHUNK_SIZE_SMALL
    elif file_size < 1024 * 1024 * 1024:  # < 1GB
        return CHUNK_SIZE_MEDIUM
    else:
        return CHUNK_SIZE_LARGE


def is_icloud_placeholder(path: Path) -> bool:
    """Check if file is an iCloud placeholder (not downloaded)."""
    # Check for .icloud prefix file
    icloud_file = path.parent / f".{path.name}.icloud"
    return icloud_file.exists() or not path.exists()


def download_from_icloud(path: Path) -> bool:
    """Force download file from iCloud using brctl."""
    try:
        logger.info(f"Downloading from iCloud: {path.name}")
        subprocess.run(
            ["brctl", "download", str(path)],
            check=True,
            capture_output=True,
            timeout=300  # 5 minute timeout
        )
        # Wait for download to complete
        max_wait = 300  # 5 minutes
        start = time.time()
        while is_icloud_placeholder(path) and (time.time() - start) < max_wait:
            time.sleep(2)
        return not is_icloud_placeholder(path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to download from iCloud: {e}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"iCloud download timed out: {path.name}")
        return False


def wait_for_file_stability(path: Path) -> bool:
    """Wait until file size stabilizes (finished writing)."""
    try:
        size1 = path.stat().st_size
        time.sleep(0.5)
        size2 = path.stat().st_size
        if size1 != size2:
            logger.debug(f"File still being written: {path.name}")
            return False
        return True
    except OSError:
        return False


def get_video_metadata(path: Path) -> dict:
    """Extract video metadata using ffprobe."""
    metadata = {"duration": None, "creation_time": None, "width": 0, "height": 0}
    
    try:
        # Get duration and dimensions
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration:stream=width,height",
                "-of", "json",
                str(path)
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            
            # Duration
            if "format" in data and "duration" in data["format"]:
                duration_sec = float(data["format"]["duration"])
                minutes = int(duration_sec // 60)
                seconds = int(duration_sec % 60)
                metadata["duration"] = f"{minutes}:{seconds:02d}"
                metadata["duration_sec"] = duration_sec
            
            # Dimensions
            if "streams" in data:
                for stream in data["streams"]:
                    if "width" in stream and "height" in stream:
                        metadata["width"] = stream["width"]
                        metadata["height"] = stream["height"]
                        break
        
        # Get creation time
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format_tags=creation_time",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path)
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0 and result.stdout.strip():
            try:
                dt = datetime.fromisoformat(result.stdout.strip().replace("Z", "+00:00"))
                # Format as "January 1st, 2025 at 2:00 PM"
                day = dt.day
                suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
                metadata["creation_time"] = dt.strftime(f"%B {day}{suffix}, %Y at %-I:%M %p")
            except ValueError:
                pass
    
    except Exception as e:
        logger.warning(f"Failed to get video metadata: {e}")
    
    return metadata


def generate_thumbnail(video_path: Path, output_path: Path) -> bool:
    """Generate thumbnail from video at 1 second mark."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "1",
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2",
                str(output_path)
            ],
            capture_output=True,
            check=True,
            timeout=30
        )
        return output_path.exists()
    except Exception as e:
        logger.warning(f"Failed to generate thumbnail: {e}")
        return False


def send_telegram_preview(filename: str, thumbnail_path: Path) -> int:
    """Send thumbnail preview to Telegram and return message_id."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_ID:
        logger.warning("Telegram credentials not configured")
        return None
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    caption = f"🎬 **{filename}**\n\n⏳ Uploading to server..."
    
    try:
        with open(thumbnail_path, "rb") as photo:
            response = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_USER_ID,
                    "caption": caption,
                    "parse_mode": "Markdown"
                },
                files={"photo": photo},
                timeout=30
            )
        
        if response.ok:
            data = response.json()
            message_id = data.get("result", {}).get("message_id")
            logger.info(f"Telegram preview sent: message_id={message_id}")
            history.log_telegram_sent(filename, message_id, int(TELEGRAM_USER_ID))
            return message_id
        else:
            logger.error(f"Telegram API error: {response.text}")
            return None
    
    except Exception as e:
        logger.error(f"Failed to send Telegram preview: {e}")
        return None


def archive_locally(video_path: Path) -> Path:
    """Copy video to local archive folder, handling duplicates."""
    archive_dir = Path(LOCAL_ARCHIVE_PATH)
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    dest_path = archive_dir / video_path.name
    
    # Handle duplicate filenames
    if dest_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = video_path.stem
        suffix = video_path.suffix
        dest_path = archive_dir / f"{stem}_{timestamp}{suffix}"
    
    shutil.copy2(video_path, dest_path)
    logger.info(f"Archived locally: {dest_path.name}")
    return dest_path


def get_upload_status(session: requests.Session, filename: str) -> int:
    """Check current upload offset from server."""
    try:
        response = session.get(
            f"{RAILWAY_URL}/upload_status",
            params={"filename": filename},
            timeout=30
        )
        if response.ok:
            return response.json().get("offset", 0)
    except Exception as e:
        logger.debug(f"Could not get upload status: {e}")
    return 0


def upload_video_chunked(session: requests.Session, video_path: Path, 
                          metadata: dict, message_id: int) -> bool:
    """Upload video in chunks with resume support."""
    filename = video_path.name
    file_size = video_path.stat().st_size
    chunk_size = get_chunk_size(file_size)
    
    logger.info(f"Starting chunked upload: {filename} ({file_size / 1024 / 1024:.1f} MB)")
    history.log_upload_started(filename, RAILWAY_URL)
    
    start_time = time.time()
    
    with open(video_path, "rb") as f:
        # Get current offset (for resume)
        offset = get_upload_status(session, filename)
        if offset > 0:
            logger.info(f"Resuming upload from offset {offset}")
            f.seek(offset)
        
        chunk_number = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            
            chunk_number += 1
            current_offset = f.tell() - len(chunk)
            
            headers = {
                "X-Filename": filename,
                "X-Total-Size": str(file_size),
                "X-Offset": str(current_offset),
                "X-Video-Duration": metadata.get("duration", ""),
                "X-Video-Creation-Time": metadata.get("creation_time", ""),
                "Content-Type": "application/octet-stream"
            }
            
            if message_id:
                headers["X-Message-Id"] = str(message_id)
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = session.post(
                        f"{RAILWAY_URL}/upload_chunk",
                        data=chunk,
                        headers=headers,
                        timeout=120
                    )
                    
                    if response.status_code == 409:
                        # Offset mismatch - get correct offset and retry
                        new_offset = response.json().get("expected_offset", 0)
                        logger.warning(f"Offset mismatch, resuming from {new_offset}")
                        f.seek(new_offset)
                        break
                    
                    if response.ok:
                        data = response.json()
                        bytes_sent = current_offset + len(chunk)
                        history.log_upload_progress(filename, bytes_sent, file_size, chunk_number)
                        
                        if data.get("status") == "complete":
                            duration = time.time() - start_time
                            logger.info(f"Upload complete: {filename} ({duration:.1f}s)")
                            history.log_upload_complete(filename, duration)
                            return True
                        break
                    else:
                        logger.error(f"Upload error: {response.status_code} - {response.text}")
                        if attempt == max_retries - 1:
                            return False
                
                except requests.RequestException as e:
                    logger.error(f"Upload request failed (attempt {attempt + 1}): {e}")
                    if attempt == max_retries - 1:
                        return False
                    time.sleep(2 ** attempt)  # Exponential backoff
    
    return True


def process_video(video_path: Path, session: requests.Session):
    """Process a single video file."""
    filename = video_path.name
    logger.info(f"Processing video: {filename}")
    
    try:
        # Check if iCloud placeholder
        if is_icloud_placeholder(video_path):
            if not download_from_icloud(video_path):
                logger.error(f"Failed to download from iCloud: {filename}")
                return
        
        # Wait for file stability
        if not wait_for_file_stability(video_path):
            logger.debug(f"File not stable yet: {filename}")
            return
        
        # Get video metadata
        metadata = get_video_metadata(video_path)
        size_mb = video_path.stat().st_size / (1024 * 1024)
        
        history.log_video_detected(
            filename=filename,
            path=str(video_path),
            size_mb=round(size_mb, 2),
            duration=metadata.get("duration"),
            creation_time=metadata.get("creation_time")
        )
        
        # Generate thumbnail
        thumbnail_path = video_path.with_suffix(".jpg")
        message_id = None
        
        if generate_thumbnail(video_path, thumbnail_path):
            message_id = send_telegram_preview(filename, thumbnail_path)
            # Clean up thumbnail
            thumbnail_path.unlink(missing_ok=True)
        
        # Archive locally before upload
        archive_locally(video_path)
        
        # Upload to server
        if upload_video_chunked(session, video_path, metadata, message_id):
            # Delete from queue after successful upload
            video_path.unlink()
            logger.info(f"Deleted from queue: {filename}")
        else:
            history.log_upload_failed(filename, "Upload failed after retries")
            logger.error(f"Failed to upload: {filename}")
    
    except Exception as e:
        log_exception(logger, f"Error processing {filename}", e)
        history.log_upload_failed(filename, str(e))


def main():
    """Main watcher loop."""
    queue_path = Path(VIDEO_QUEUE_PATH)
    
    # Validate configuration
    if not RAILWAY_URL:
        logger.error("RAILWAY_URL not configured")
        sys.exit(1)
    
    logger.info(f"🎬 YT Video Uploader Watcher started")
    logger.info(f"Watching: {queue_path}")
    logger.info(f"Server: {RAILWAY_URL}")
    
    # Ensure queue directory exists
    queue_path.mkdir(parents=True, exist_ok=True)
    
    # Create session with connection pooling
    session = create_session()
    
    # Track processed files to avoid reprocessing
    processed_files = set()
    
    while True:
        try:
            # Scan for video files
            for item in queue_path.iterdir():
                # Skip non-video files and already processed
                if item.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if item.name.startswith("."):
                    continue
                if item in processed_files:
                    continue
                
                # Process video
                processed_files.add(item)
                process_video(item, session)
            
            # Clean up processed set (remove files that no longer exist)
            processed_files = {f for f in processed_files if f.exists()}
        
        except Exception as e:
            log_exception(logger, "Error in main loop", e)
        
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
