#!/usr/bin/env python3
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG_DIR = Path.home() / ".config/queue"
CRED_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

PORT = 8080
REDIRECT_URI = f"http://localhost:{PORT}/oauth2callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"

auth_code = None

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <body style="font-family:sans-serif; text-align:center; padding-top:50px;">
                    <h2 style="color:#2e7d32;">&#10004; Authorization Successful</h2>
                    <p>You can close this tab and return to your terminal.</p>
                </body>
                </html>
            """)
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *args):
        pass

def main():
    if not CRED_FILE.exists():
        print(f"Error: Missing credentials file at {CRED_FILE}", file=sys.stderr)
        print("Please place credentials.json in ~/.config/queue/", file=sys.stderr)
        sys.exit(1)

    with open(CRED_FILE) as f:
        data = json.load(f)
        creds = data.get("installed") or data.get("web") or data
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]

    query_params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent"
    })

    server = HTTPServer(("localhost", PORT), OAuthCallbackHandler)
    print(f"Listening for OAuth callback on {REDIRECT_URI} ...")
    print(f"Opening browser for Google authorization...")
    webbrowser.open(f"{AUTH_URL}?{query_params}")

    while auth_code is None:
        server.handle_request()
    server.server_close()

    print("Authorization code received! Exchanging for tokens...")
    payload = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read().decode())

    # Store client credentials alongside the refresh token for automated refresh
    tokens["client_id"] = client_id
    tokens["client_secret"] = client_secret

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)

    print(f"Tokens saved successfully to {TOKEN_FILE}")

if __name__ == "__main__":
    main()