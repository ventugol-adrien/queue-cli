#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

CONFIG_DIR = Path.home() / ".config/queue"
TOKEN_FILE = CONFIG_DIR / "token.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

def get_access_token():
    if not TOKEN_FILE.exists():
        print(f"Error: No token found at {TOKEN_FILE}", file=sys.stderr)
        print("Please run auth.py first to authorize your Gmail account.", file=sys.stderr)
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        tokens = json.load(f)

    # Silent token refresh using stored refresh_token
    payload = urllib.parse.urlencode({
        "client_id": tokens["client_id"],
        "client_secret": tokens["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token"
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return data["access_token"]
    except Exception as e:
        print(f"Error refreshing access token: {e}", file=sys.stderr)
        sys.exit(1)

def send_mail(to_addr, subject, body, attachment_path=None):
    access_token = get_access_token()

    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_path and os.path.isfile(attachment_path):
        mime_type, _ = mimetypes.guess_type(attachment_path)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        with open(attachment_path, "rb") as f:
            file_data = f.read()
        msg.add_attachment(
            file_data,
            maintype=maintype,
            subtype=subtype,
            filename=Path(attachment_path).name
        )

    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = json.dumps({"raw": raw_msg}).encode()

    req = urllib.request.Request(SEND_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                print(f"Notification sent successfully to {to_addr}")
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    """
    Main cli parser
    """
    parser = argparse.ArgumentParser(description="Standalone Gmail API Dispatcher")
    parser.add_argument("-t", "--to", required=True, help="Recipient email address")
    parser.add_argument("-s", "--subject", required=True, help="Email subject line")
    parser.add_argument("-b", "--body", default="Task completed successfully.", help="Email body")
    parser.add_argument("-a", "--attach", default=None, help="Path to file attachment")

    args = parser.parse_args()
    send_mail(args.to, args.subject, args.body, args.attach)

if __name__ == "__main__":
    main()
