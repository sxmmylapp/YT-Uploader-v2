# YT Video Uploader v2 Workflow

## System Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   iCloud Drive  │────▶│  Mac (local)     │────▶│  Railway Server │
│   VideoQueue_v2/│     │  watch_icloud.py │     │  server.py      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                         │
                                                         ▼
                                                 ┌───────────────┐
                                                 │   Telegram    │
                                                 │   Bot (user)  │
                                                 └───────┬───────┘
                                                         │
                                                         ▼
                                                 ┌───────────────┐
                                                 │   YouTube     │
                                                 │   (upload)    │
                                                 └───────────────┘
```

## Components

1. **watch_icloud.py** - Monitors `~/Library/Mobile Documents/com~apple~CloudDocs/VideoQueue_v2/`
2. **server.py** - Flask app on Railway handling uploads, Telegram webhooks, YouTube uploads
3. **LaunchAgent** - Keeps watcher running as background service

## Quick Start

### 1. Setup Local Environment
```bash
cd "/Users/sammylapp/.gemini/antigravity/Workspaces/YT Uploader v2"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
cp .env.template .env
# Edit .env with your values
```

### 3. Generate YouTube OAuth Credentials
```bash
python execution/get_credentials.py
```

### 4. Install LaunchAgent
```bash
cp com.ytuploader.watcher.v2.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ytuploader.watcher.v2.plist
```

### 5. Deploy to Railway
1. Push code to GitHub
2. Connect Railway to repo
3. Set environment variables in Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_USER_ID`
   - `GOOGLE_CREDENTIALS` (output from get_credentials.py)

## Usage

1. Drop video files into `~/Library/Mobile Documents/com~apple~CloudDocs/VideoQueue_v2/`
2. Receive Telegram notification with thumbnail
3. Reply with video title
4. Select privacy level (Public/Unlisted/Private)
5. Confirm upload
6. Video uploads to YouTube automatically
