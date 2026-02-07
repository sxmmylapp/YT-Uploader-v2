"""
Logging configuration for YT Video Uploader.

Provides:
- VideoHistoryLogger: JSONL audit trail for video processing
- setup_watcher_logging(): Configure all loggers for the watcher script
- log_exception(): Helper for logging exceptions with full traceback
"""

import os
import json
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class VideoHistoryLogger:
    """JSONL audit trail for video processing events."""
    
    def __init__(self, log_dir: str = "logs/history"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "video_history.jsonl"
    
    def _write_entry(self, event_type: str, **kwargs):
        """Write a JSONL entry to the history log."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event_type,
            **kwargs
        }
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_video_detected(self, filename: str, path: str, size_mb: float, 
                           duration: str = None, creation_time: str = None):
        """Log when a new video is detected in the queue."""
        self._write_entry(
            "video_detected",
            filename=filename,
            path=path,
            size_mb=size_mb,
            duration=duration,
            creation_time=creation_time
        )
    
    def log_upload_started(self, filename: str, server_url: str):
        """Log when upload to server begins."""
        self._write_entry("upload_started", filename=filename, server_url=server_url)
    
    def log_upload_progress(self, filename: str, bytes_sent: int, 
                            total_bytes: int, chunk_number: int):
        """Log upload progress (every chunk or at intervals)."""
        self._write_entry(
            "upload_progress",
            filename=filename,
            bytes_sent=bytes_sent,
            total_bytes=total_bytes,
            chunk_number=chunk_number,
            percent=round((bytes_sent / total_bytes) * 100, 1) if total_bytes > 0 else 0
        )
    
    def log_upload_complete(self, filename: str, duration_seconds: float):
        """Log successful upload completion."""
        self._write_entry(
            "upload_complete",
            filename=filename,
            duration_seconds=round(duration_seconds, 2)
        )
    
    def log_upload_failed(self, filename: str, error: str):
        """Log upload failure."""
        self._write_entry("upload_failed", filename=filename, error=error)
    
    def log_telegram_sent(self, filename: str, message_id: int, chat_id: int):
        """Log when Telegram preview message is sent."""
        self._write_entry(
            "telegram_sent",
            filename=filename,
            message_id=message_id,
            chat_id=chat_id
        )
    
    def log_telegram_updated(self, filename: str, message_id: int, 
                             update_type: str, status: str = None):
        """Log when Telegram message is updated."""
        self._write_entry(
            "telegram_updated",
            filename=filename,
            message_id=message_id,
            update_type=update_type,
            status=status
        )


class JSONFormatter(logging.Formatter):
    """Format log records as JSON for structured logging."""
    
    def format(self, record):
        log_obj = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def setup_watcher_logging(log_dir: str = "logs/watcher"):
    """
    Set up logging for the watcher script.
    
    Returns:
        tuple: (logger, VideoHistoryLogger)
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Create main logger
    logger = logging.getLogger("watcher")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    
    # Standard format for text logs
    text_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Main log - INFO level, daily rotation, keep 7 days
    main_handler = TimedRotatingFileHandler(
        log_path / "watcher.log",
        when="midnight",
        interval=1,
        backupCount=7
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(text_format)
    logger.addHandler(main_handler)
    
    # Error log - ERROR level, daily rotation, keep 30 days
    error_handler = TimedRotatingFileHandler(
        log_path / "watcher_errors.log",
        when="midnight",
        interval=1,
        backupCount=30
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(text_format)
    logger.addHandler(error_handler)
    
    # Debug log - DEBUG level, daily rotation, keep 3 days
    debug_handler = TimedRotatingFileHandler(
        log_path / "watcher_debug.log",
        when="midnight",
        interval=1,
        backupCount=3
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(text_format)
    logger.addHandler(debug_handler)
    
    # JSON structured log for programmatic analysis
    json_handler = TimedRotatingFileHandler(
        log_path / "watcher_structured.jsonl",
        when="midnight",
        interval=1,
        backupCount=7
    )
    json_handler.setLevel(logging.INFO)
    json_handler.setFormatter(JSONFormatter())
    logger.addHandler(json_handler)
    
    # Console output - INFO level
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(text_format)
    logger.addHandler(console_handler)
    
    # Create history logger
    history = VideoHistoryLogger()
    
    return logger, history


def log_exception(logger: logging.Logger, message: str, exc: Exception):
    """Helper to log exceptions with full traceback."""
    logger.exception(f"{message}: {exc}")
