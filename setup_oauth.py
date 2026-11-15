"""
Helper script to generate GMAIL_REFRESH_TOKEN and detect MY_EMAIL.
"""

import os
import sys
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.stdout.reconfigure(line_buffering=True)
load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send'
]

def main():
    print("="*60, flush=True)
    print(" GMAIL OAUTH REFRESH TOKEN & PROFILE DETECTOR", flush=True)
    print("="*60, flush=True)
    
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("[!] GMAIL_CLIENT_ID or GMAIL_CLIENT_SECRET not found in .env.", flush=True)
        sys.exit(1)
        
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/", "http://localhost"]
        }
    }
    
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    
    # Save URL to file so user or assistant can easily read it
    with open("oauth_url.txt", "w", encoding="utf-8") as f:
        f.write(auth_url)
        
    print("\n>>> Open this URL to authorize Gmail access:", flush=True)
    print(auth_url, flush=True)
    print("\n>>> Waiting for your login approval on http://localhost:8080/ ...", flush=True)
    
    creds = flow.run_local_server(port=8080, access_type='offline', prompt='consent')
    
    # Automatically retrieve email address from profile!
    detected_email = ""
    try:
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        detected_email = profile.get('emailAddress', '')
        print(f"[✓] Detected Gmail Address: {detected_email}", flush=True)
    except Exception as e:
        print(f"Could not auto-fetch email: {e}", flush=True)

    print("\n" + "="*60, flush=True)
    print(" SUCCESS! Credentials Obtained:", flush=True)
    print("="*60, flush=True)
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}", flush=True)
    if detected_email:
        print(f"MY_EMAIL={detected_email}", flush=True)
    print("="*60, flush=True)

    # Reusable auto-update of local .env file
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        import re
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        content = re.sub(r"GMAIL_REFRESH_TOKEN=.*", f"GMAIL_REFRESH_TOKEN={creds.refresh_token}", content)
        if detected_email:
            content = re.sub(r"MY_EMAIL=.*", f"MY_EMAIL={detected_email}", content)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[✓] Automatically updated your local .env file!", flush=True)

    if os.path.exists("oauth_url.txt"):
        try:
            os.remove("oauth_url.txt")
        except Exception:
            pass

if __name__ == "__main__":
    main()
