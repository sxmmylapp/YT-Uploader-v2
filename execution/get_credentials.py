#!/usr/bin/env python3
"""
OAuth Helper Script

Run this locally to generate YouTube OAuth credentials.
The output can be used as the GOOGLE_CREDENTIALS environment variable on Railway.
"""

import os
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# Scopes required for YouTube upload
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive"
]

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
CLIENT_SECRETS_FILE = PROJECT_ROOT / "client_secrets.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"


def main():
    """Run OAuth flow and save/print credentials."""
    creds = None
    
    # Load existing token if available
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    
    # If no valid credentials, run OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_FILE.exists():
                print(f"Error: {CLIENT_SECRETS_FILE} not found!")
                print("\nTo set up OAuth:")
                print("1. Go to https://console.cloud.google.com/apis/credentials")
                print("2. Create OAuth 2.0 Client ID (Desktop application)")
                print("3. Download the JSON and save as 'client_secrets.json'")
                return
            
            print("Starting OAuth flow...")
            print("A browser window will open for authentication.")
            
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS_FILE),
                SCOPES
            )
            creds = flow.run_local_server(port=8080)
    
    # Save token to file
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"\n✅ Token saved to: {TOKEN_FILE}")
    
    # Print credentials for Railway
    creds_json = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes)
    }
    
    print("\n" + "=" * 60)
    print("GOOGLE_CREDENTIALS for Railway:")
    print("=" * 60)
    print(json.dumps(creds_json))
    print("=" * 60)
    print("\nCopy the JSON above and set it as GOOGLE_CREDENTIALS env var on Railway.")


if __name__ == "__main__":
    main()
