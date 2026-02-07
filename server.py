#!/usr/bin/env python3
"""
YT Video Uploader Server

Flask web application for handling video uploads, Telegram webhooks, and YouTube uploads.
Designed for Railway deployment.
"""

import os
import json
import time
import threading
import asyncio
import tempfile
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

# Google APIs
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
TELEGRAM_BROTHER_ID = os.getenv("TELEGRAM_BROTHER_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

# Storage paths
UPLOAD_DIR = Path(tempfile.gettempdir()) / "yt_uploads"
STATE_FILE = UPLOAD_DIR / "video_state.json"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory state
pending_videos = {}
partial_uploads = {}  # filename -> {path, offset, total_size}
upload_lock = threading.Lock()

# Video states
STATE_AWAITING_TITLE = "awaiting_title"
STATE_AWAITING_PRIVACY = "awaiting_privacy"
STATE_READY_TO_UPLOAD = "ready_to_upload"
STATE_UPLOADING = "uploading"


# ============== State Management ==============

def save_state():
    """Persist state to JSON file."""
    with upload_lock:
        state = {
            "pending_videos": pending_videos,
            "partial_uploads": {k: {"offset": v["offset"], "total_size": v["total_size"]} 
                               for k, v in partial_uploads.items()}
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=str)


def load_state():
    """Load state from JSON file."""
    global pending_videos, partial_uploads
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
                pending_videos = state.get("pending_videos", {})
                # Restore partial uploads (without file handles)
                for k, v in state.get("partial_uploads", {}).items():
                    partial_uploads[k] = v
        except Exception as e:
            app.logger.error(f"Failed to load state: {e}")


def generate_video_id(filename: str) -> str:
    """Generate unique video ID."""
    import hashlib
    timestamp = datetime.now().isoformat()
    return hashlib.md5(f"{filename}{timestamp}".encode()).hexdigest()[:12]


# ============== Telegram Helpers ==============

def send_telegram_message(chat_id: int, text: str, reply_markup=None) -> dict:
    """Send a Telegram message."""
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    
    response = requests.post(url, json=data, timeout=30)
    return response.json()


def edit_telegram_message(chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
    """Edit an existing Telegram message."""
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    
    for attempt in range(5):
        try:
            response = requests.post(url, json=data, timeout=30)
            if response.ok:
                return True
            # Handle "message is not modified" error gracefully
            if "message is not modified" in response.text:
                return True
        except Exception as e:
            app.logger.error(f"Edit message attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)
    
    return False


def edit_telegram_caption(chat_id: int, message_id: int, caption: str, reply_markup=None) -> bool:
    """Edit caption of a message with media."""
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageCaption"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": "HTML"
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    
    response = requests.post(url, json=data, timeout=30)
    return response.ok


def answer_callback_query(callback_query_id: str, text: str = None):
    """Answer a callback query."""
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text
    requests.post(url, json=data, timeout=10)


def create_privacy_keyboard(video_id: str):
    """Create privacy selection keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "🌍 Public", "callback_data": f"privacy:public:{video_id}"},
                {"text": "🔗 Unlisted", "callback_data": f"privacy:unlisted:{video_id}"},
                {"text": "🔒 Private", "callback_data": f"privacy:private:{video_id}"}
            ]
        ]
    }


def create_upload_keyboard(video_id: str):
    """Create upload confirmation keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Upload", "callback_data": f"action:yes:{video_id}"},
                {"text": "❌ Delete", "callback_data": f"action:no:{video_id}"}
            ]
        ]
    }


def create_delete_confirmation_keyboard(video_id: str):
    """Create delete confirmation keyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "⚠️ Yes, Delete", "callback_data": f"confirm:yes:{video_id}"},
                {"text": "↩️ Go Back", "callback_data": f"confirm:no:{video_id}"}
            ]
        ]
    }


# ============== YouTube Helpers ==============

def get_youtube_service():
    """Get authenticated YouTube service with automatic token refresh."""
    if not GOOGLE_CREDENTIALS:
        raise ValueError("GOOGLE_CREDENTIALS not configured")
    
    try:
        creds_data = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", [
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly"
            ])
        )
        
        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Update env var with new token (for Railway)
            updated_creds = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes) if creds.scopes else []
            }
            os.environ["GOOGLE_CREDENTIALS"] = json.dumps(updated_creds)
        
        return build("youtube", "v3", credentials=creds)
    
    except Exception as e:
        app.logger.error(f"Failed to create YouTube service: {e}")
        raise


def check_portrait_video(video_path: Path) -> bool:
    """Check if video is portrait (height > width)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data.get("streams"):
                stream = data["streams"][0]
                return stream.get("height", 0) > stream.get("width", 0)
    except Exception as e:
        app.logger.warning(f"Could not check video orientation: {e}")
    return False


def rotate_video(video_path: Path) -> Path:
    """Rotate video 90° clockwise for portrait videos."""
    rotated_path = video_path.with_name(f"rotated_{video_path.name}")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vf", "transpose=1",
             "-c:a", "copy", str(rotated_path)],
            capture_output=True, check=True, timeout=600
        )
        return rotated_path
    except Exception as e:
        app.logger.error(f"Failed to rotate video: {e}")
        return video_path


def upload_to_youtube(video_id: str):
    """Upload video to YouTube in background thread."""
    video = pending_videos.get(video_id)
    if not video:
        return
    
    chat_id = video.get("chat_id", TELEGRAM_USER_ID)
    message_id = video.get("message_id")
    
    try:
        # Update status
        video["state"] = STATE_UPLOADING
        save_state()
        edit_telegram_message(chat_id, message_id, "⏳ Uploading to YouTube...")
        
        video_path = Path(video["path"])
        
        # Check and rotate portrait videos
        if check_portrait_video(video_path):
            app.logger.info(f"Rotating portrait video: {video_path.name}")
            video_path = rotate_video(video_path)
        
        youtube = get_youtube_service()
        
        # Prepare metadata
        title = video.get("title", video_path.stem)[:100]
        description = f"Original filename: {video['filename']}"
        if video.get("creation_time"):
            description += f"\nRecorded: {video['creation_time']}"
        
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": ["volleyball", "vball"],
                "categoryId": "17"  # Sports
            },
            "status": {
                "privacyStatus": video.get("privacy", "unlisted"),
                "selfDeclaredMadeForKids": False
            }
        }
        
        # Upload with progress
        media = MediaFileUpload(str(video_path), resumable=True, chunksize=10*1024*1024)
        upload_request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        last_progress_update = 0
        
        while response is None:
            status, response = upload_request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                # Update every 5% or every 5 seconds
                if progress - last_progress_update >= 5 or time.time() - last_progress_update > 5:
                    bar_filled = int(progress / 10)
                    bar_empty = 10 - bar_filled
                    progress_bar = "▓" * bar_filled + "░" * bar_empty
                    edit_telegram_message(
                        chat_id, message_id,
                        f"⏳ Uploading to YouTube...\n\n{progress_bar} {progress}%"
                    )
                    last_progress_update = progress
        
        youtube_id = response.get("id")
        youtube_url = f"https://youtu.be/{youtube_id}"
        
        # Check for immediate rejection
        video_status = youtube.videos().list(part="status", id=youtube_id).execute()
        if video_status.get("items"):
            status_detail = video_status["items"][0].get("status", {})
            rejection = status_detail.get("rejectionReason")
            if rejection:
                edit_telegram_message(
                    chat_id, message_id,
                    f"⚠️ <b>Upload Rejected</b>\n\nReason: {rejection}"
                )
                return
        
        # Poll for processing completion
        edit_telegram_message(chat_id, message_id, "⏳ Processing on YouTube...")
        
        max_poll_time = 600  # 10 minutes
        poll_start = time.time()
        
        while time.time() - poll_start < max_poll_time:
            time.sleep(30)
            try:
                status_response = youtube.videos().list(part="status,processingDetails", id=youtube_id).execute()
                if status_response.get("items"):
                    item = status_response["items"][0]
                    processing = item.get("processingDetails", {})
                    if processing.get("processingStatus") == "succeeded":
                        break
            except Exception as e:
                app.logger.warning(f"Poll error: {e}")
        
        # Success message
        edit_telegram_message(
            chat_id, message_id,
            f"✅ <b>Ready to Watch!</b>\n\n🎬 {title}\n\n🔗 {youtube_url}"
        )
        
        # Notify brother if video > 10 minutes
        duration_sec = video.get("duration_sec", 0)
        if duration_sec > 600 and TELEGRAM_BROTHER_ID:
            send_telegram_message(
                int(TELEGRAM_BROTHER_ID),
                f"🎬 New video uploaded!\n\n<b>{title}</b>\n\n🔗 {youtube_url}"
            )
        
        # Clean up
        video_path.unlink(missing_ok=True)
        if video_id in pending_videos:
            del pending_videos[video_id]
        save_state()
    
    except Exception as e:
        app.logger.exception(f"YouTube upload failed: {e}")
        edit_telegram_message(
            chat_id, message_id,
            f"❌ <b>Upload Failed</b>\n\nError: {str(e)[:200]}"
        )


# ============== Flask Routes ==============

@app.route("/")
def index():
    """Health check endpoint."""
    return jsonify({
        "status": "running",
        "pending_videos": len(pending_videos)
    })


@app.route("/upload_status", methods=["GET"])
def upload_status():
    """Get current upload offset for resume."""
    filename = request.args.get("filename")
    if filename and filename in partial_uploads:
        return jsonify({"offset": partial_uploads[filename].get("offset", 0)})
    return jsonify({"offset": 0})


@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    """Handle chunked video upload."""
    filename = request.headers.get("X-Filename")
    total_size = int(request.headers.get("X-Total-Size", 0))
    offset = int(request.headers.get("X-Offset", 0))
    duration = request.headers.get("X-Video-Duration", "")
    creation_time = request.headers.get("X-Video-Creation-Time", "")
    message_id = request.headers.get("X-Message-Id")
    
    if not filename:
        return jsonify({"error": "Missing filename"}), 400
    
    # Check offset
    if filename in partial_uploads:
        expected_offset = partial_uploads[filename].get("offset", 0)
        if offset != expected_offset:
            return jsonify({"error": "Offset mismatch", "expected_offset": expected_offset}), 409
    
    # Write chunk
    file_path = UPLOAD_DIR / filename
    mode = "ab" if offset > 0 else "wb"
    
    with open(file_path, mode) as f:
        chunk_data = request.get_data()
        f.write(chunk_data)
    
    new_offset = offset + len(chunk_data)
    partial_uploads[filename] = {"offset": new_offset, "total_size": total_size}
    
    # Check if complete
    if new_offset >= total_size:
        # Create pending video entry
        video_id = generate_video_id(filename)
        
        # Check for duplicates
        for vid, vdata in pending_videos.items():
            if vdata.get("filename") == filename:
                # Update existing entry
                video_id = vid
                break
        
        pending_videos[video_id] = {
            "path": str(file_path),
            "filename": filename,
            "size_mb": round(total_size / (1024 * 1024), 2),
            "uploaded_at": datetime.now().isoformat(),
            "creation_time": creation_time,
            "duration": duration,
            "state": STATE_AWAITING_TITLE,
            "chat_id": int(TELEGRAM_USER_ID) if TELEGRAM_USER_ID else None,
            "message_id": int(message_id) if message_id else None
        }
        
        del partial_uploads[filename]
        save_state()
        
        # Update Telegram message
        if message_id:
            edit_telegram_caption(
                int(TELEGRAM_USER_ID), int(message_id),
                f"🎬 <b>{filename}</b>\n\n💬 Reply with a title for this video",
                None
            )
        
        return jsonify({"status": "complete", "video_id": video_id})
    
    save_state()
    return jsonify({"status": "partial", "offset": new_offset})


@app.route("/upload", methods=["POST"])
def upload_direct():
    """Direct multipart upload (fallback)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    filename = file.filename
    file_path = UPLOAD_DIR / filename
    file.save(file_path)
    
    video_id = generate_video_id(filename)
    pending_videos[video_id] = {
        "path": str(file_path),
        "filename": filename,
        "size_mb": round(file_path.stat().st_size / (1024 * 1024), 2),
        "uploaded_at": datetime.now().isoformat(),
        "state": STATE_AWAITING_TITLE,
        "chat_id": int(TELEGRAM_USER_ID) if TELEGRAM_USER_ID else None
    }
    
    save_state()
    
    # Send Telegram notification
    send_telegram_message(
        int(TELEGRAM_USER_ID),
        f"🎬 <b>New Video Uploaded</b>\n\n{filename}\n\n💬 Reply with a title"
    )
    
    return jsonify({"status": "complete", "video_id": video_id})


@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Handle Telegram webhook updates."""
    data = request.get_json()
    
    # Handle callback queries (button presses)
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback["id"]
        callback_data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        
        parts = callback_data.split(":")
        action = parts[0] if len(parts) > 0 else ""
        value = parts[1] if len(parts) > 1 else ""
        video_id = parts[2] if len(parts) > 2 else ""
        
        # Answer callback immediately
        answer_callback_query(callback_id)
        
        # Handle privacy selection
        if action == "privacy" and video_id in pending_videos:
            pending_videos[video_id]["privacy"] = value
            pending_videos[video_id]["state"] = STATE_READY_TO_UPLOAD
            save_state()
            
            video = pending_videos[video_id]
            privacy_emoji = {"public": "🌍", "unlisted": "🔗", "private": "🔒"}.get(value, "")
            
            edit_telegram_message(
                chat_id, message_id,
                f"🎬 <b>{video.get('title', video['filename'])}</b>\n\n"
                f"{privacy_emoji} Privacy: {value.title()}\n\n"
                f"Ready to upload?",
                create_upload_keyboard(video_id)
            )
        
        # Handle upload confirmation
        elif action == "action" and value == "yes" and video_id in pending_videos:
            pending_videos[video_id]["chat_id"] = chat_id
            pending_videos[video_id]["message_id"] = message_id
            save_state()
            
            # Start upload in background thread
            thread = threading.Thread(target=upload_to_youtube, args=(video_id,))
            thread.daemon = True
            thread.start()
        
        # Handle delete button
        elif action == "action" and value == "no" and video_id in pending_videos:
            edit_telegram_message(
                chat_id, message_id,
                "⚠️ <b>Delete this video?</b>\n\nThis action cannot be undone.",
                create_delete_confirmation_keyboard(video_id)
            )
        
        # Handle delete confirmation
        elif action == "confirm" and value == "yes" and video_id in pending_videos:
            video = pending_videos[video_id]
            Path(video["path"]).unlink(missing_ok=True)
            del pending_videos[video_id]
            save_state()
            
            edit_telegram_message(chat_id, message_id, "🗑️ Video deleted.")
        
        # Handle delete cancel (go back)
        elif action == "confirm" and value == "no" and video_id in pending_videos:
            video = pending_videos[video_id]
            edit_telegram_message(
                chat_id, message_id,
                f"🎬 <b>{video.get('title', video['filename'])}</b>\n\n"
                f"Select privacy level:",
                create_privacy_keyboard(video_id)
            )
        
        # Handle cleanup confirmations
        elif action == "cleanup" and value == "yes":
            count = len(pending_videos)
            for vid, vdata in list(pending_videos.items()):
                Path(vdata["path"]).unlink(missing_ok=True)
            pending_videos.clear()
            save_state()
            edit_telegram_message(chat_id, message_id, f"🗑️ Deleted {count} videos.")
        
        elif action == "cleanup" and value == "no":
            edit_telegram_message(chat_id, message_id, "❌ Cleanup cancelled.")
    
    # Handle text messages
    elif "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")
        
        # Handle commands
        if text.startswith("/"):
            command = text.split()[0].lower()
            
            if command == "/start":
                send_telegram_message(
                    chat_id,
                    "👋 <b>YT Video Uploader</b>\n\n"
                    "Drop videos in your iCloud VideoQueue_v2 folder and I'll help upload them!\n\n"
                    "Commands:\n"
                    "/check - Bot status\n"
                    "/pending - List pending videos\n"
                    "/cleanup - Delete all pending"
                )
            
            elif command == "/check":
                send_telegram_message(
                    chat_id,
                    f"✅ Bot is running\n\n📹 Pending videos: {len(pending_videos)}"
                )
            
            elif command == "/pending":
                if not pending_videos:
                    send_telegram_message(chat_id, "📭 No pending videos")
                else:
                    lines = ["📹 <b>Pending Videos:</b>\n"]
                    for vid, v in pending_videos.items():
                        lines.append(f"• {v['filename']} ({v['state']})")
                    send_telegram_message(chat_id, "\n".join(lines))
            
            elif command == "/cleanup":
                if not pending_videos:
                    send_telegram_message(chat_id, "📭 No videos to clean up")
                else:
                    send_telegram_message(
                        chat_id,
                        f"⚠️ Delete all {len(pending_videos)} pending videos?",
                        {
                            "inline_keyboard": [[
                                {"text": "✅ Yes", "callback_data": "cleanup:yes:all"},
                                {"text": "❌ No", "callback_data": "cleanup:no:all"}
                            ]]
                        }
                    )
        
        # Handle replies (for setting title)
        elif "reply_to_message" in message:
            reply_msg_id = message["reply_to_message"]["message_id"]
            
            # Find video by message_id
            for vid, v in pending_videos.items():
                if v.get("message_id") == reply_msg_id and v["state"] == STATE_AWAITING_TITLE:
                    v["title"] = text.strip()[:100]
                    v["state"] = STATE_AWAITING_PRIVACY
                    save_state()
                    
                    edit_telegram_caption(
                        chat_id, reply_msg_id,
                        f"🎬 <b>{v['title']}</b>\n\nSelect privacy level:",
                        create_privacy_keyboard(vid)
                    )
                    break
    
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def server_status():
    """Detailed server status."""
    return jsonify({
        "status": "running",
        "pending_count": len(pending_videos),
        "pending_videos": [
            {
                "id": vid,
                "filename": v["filename"],
                "state": v["state"],
                "size_mb": v.get("size_mb")
            }
            for vid, v in pending_videos.items()
        ]
    })


@app.route("/pending", methods=["GET"])
def list_pending():
    """List all pending videos."""
    return jsonify(list(pending_videos.values()))


@app.route("/preview/<video_id>", methods=["GET"])
def preview_video(video_id):
    """Serve video file for preview."""
    if video_id not in pending_videos:
        return jsonify({"error": "Video not found"}), 404
    
    video_path = Path(pending_videos[video_id]["path"])
    if not video_path.exists():
        return jsonify({"error": "File not found"}), 404
    
    return send_file(video_path, mimetype="video/mp4")


@app.route("/delete/<video_id>", methods=["POST", "DELETE"])
def delete_video(video_id):
    """Delete a specific pending video."""
    if video_id not in pending_videos:
        return jsonify({"error": "Video not found"}), 404
    
    video = pending_videos[video_id]
    Path(video["path"]).unlink(missing_ok=True)
    del pending_videos[video_id]
    save_state()
    
    return jsonify({"status": "deleted"})


@app.route("/retry_notify/<video_id>", methods=["POST"])
def retry_notify(video_id):
    """Re-send Telegram notification for a video."""
    if video_id not in pending_videos:
        return jsonify({"error": "Video not found"}), 404
    
    video = pending_videos[video_id]
    send_telegram_message(
        int(TELEGRAM_USER_ID),
        f"🎬 <b>{video['filename']}</b>\n\n💬 Reply with a title"
    )
    
    return jsonify({"status": "notification_sent"})


@app.route("/cleanup", methods=["POST"])
def cleanup_all():
    """Delete all pending videos."""
    count = len(pending_videos)
    for vid, v in list(pending_videos.items()):
        Path(v["path"]).unlink(missing_ok=True)
    pending_videos.clear()
    save_state()
    
    return jsonify({"status": "cleaned", "deleted": count})


@app.route("/cleanup_stale", methods=["POST"])
def cleanup_stale():
    """Delete videos older than 7 days."""
    cutoff = datetime.now() - timedelta(days=7)
    deleted = 0
    
    for vid, v in list(pending_videos.items()):
        try:
            uploaded_at = datetime.fromisoformat(v["uploaded_at"])
            if uploaded_at < cutoff:
                Path(v["path"]).unlink(missing_ok=True)
                del pending_videos[vid]
                deleted += 1
        except Exception:
            pass
    
    save_state()
    return jsonify({"status": "cleaned", "deleted": deleted})


@app.route("/debug/config", methods=["GET"])
def debug_config():
    """Check environment variable configuration."""
    return jsonify({
        "telegram_bot_token": bool(TELEGRAM_BOT_TOKEN),
        "telegram_user_id": bool(TELEGRAM_USER_ID),
        "telegram_brother_id": bool(TELEGRAM_BROTHER_ID),
        "google_credentials": bool(GOOGLE_CREDENTIALS),
        "webhook_secret": bool(WEBHOOK_SECRET)
    })


@app.route("/uploaded_today", methods=["GET"])
def uploaded_today():
    """List videos uploaded to YouTube in last 24 hours."""
    # This would require tracking completed uploads - for now return empty
    return jsonify({"videos": []})


@app.route("/recent_videos", methods=["GET"])
def recent_videos():
    """List recent YouTube videos."""
    try:
        youtube = get_youtube_service()
        response = youtube.channels().list(part="contentDetails", mine=True).execute()
        
        if not response.get("items"):
            return jsonify({"videos": []})
        
        uploads_id = response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        
        playlist_response = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_id,
            maxResults=10
        ).execute()
        
        videos = []
        for item in playlist_response.get("items", []):
            snippet = item["snippet"]
            videos.append({
                "title": snippet["title"],
                "video_id": snippet["resourceId"]["videoId"],
                "url": f"https://youtu.be/{snippet['resourceId']['videoId']}",
                "published_at": snippet["publishedAt"]
            })
        
        return jsonify({"videos": videos})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== Background Threads ==============

def stale_cleanup_thread():
    """Background thread to clean up stale videos."""
    while True:
        time.sleep(86400)  # 24 hours
        app.logger.info("Running stale cleanup...")
        cleanup_stale()


def pending_reminder_thread():
    """Background thread to remind about pending videos."""
    while True:
        time.sleep(3600)  # 1 hour
        
        if not pending_videos:
            continue
        
        old_videos = []
        cutoff = datetime.now() - timedelta(hours=1)
        
        for vid, v in pending_videos.items():
            try:
                uploaded_at = datetime.fromisoformat(v["uploaded_at"])
                if uploaded_at < cutoff and v["state"] != STATE_UPLOADING:
                    old_videos.append(v["filename"])
            except Exception:
                pass
        
        if old_videos:
            send_telegram_message(
                int(TELEGRAM_USER_ID),
                f"📢 <b>Reminder:</b> You have {len(old_videos)} pending video(s):\n\n" +
                "\n".join(f"• {f}" for f in old_videos[:5]) +
                (f"\n... and {len(old_videos) - 5} more" if len(old_videos) > 5 else "")
            )


def register_webhook():
    """Register Telegram webhook on startup."""
    import requests
    
    # Get Railway URL from environment
    railway_url = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_URL", "")
    if not railway_url:
        app.logger.warning("No Railway URL configured, skipping webhook registration")
        return
    
    if not railway_url.startswith("http"):
        railway_url = f"https://{railway_url}"
    
    webhook_url = f"{railway_url.rstrip('/')}/webhook"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    data = {"url": webhook_url}
    
    if WEBHOOK_SECRET:
        data["secret_token"] = WEBHOOK_SECRET
    
    try:
        response = requests.post(url, json=data, timeout=30)
        if response.ok:
            app.logger.info(f"Webhook registered: {webhook_url}")
        else:
            app.logger.error(f"Failed to register webhook: {response.text}")
    except Exception as e:
        app.logger.error(f"Webhook registration error: {e}")


# ============== Startup ==============

def startup():
    """Initialize server on startup."""
    load_state()
    
    # Start background threads
    threading.Thread(target=stale_cleanup_thread, daemon=True).start()
    threading.Thread(target=pending_reminder_thread, daemon=True).start()
    
    # Register webhook
    if TELEGRAM_BOT_TOKEN:
        register_webhook()
    
    app.logger.info("🚀 YT Video Uploader server started")


# Run startup on import (for gunicorn)
startup()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
