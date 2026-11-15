# ⚡ InboxCura AI Workspace

A premium, minimal email workspace & executive briefing engine featuring the **InboxCura** frontend (Dashboard, AI Assistant, and Gmail-style Inbox), backed by **TiDB Serverless** and **Google Gemini 3.5 Flash**.

Built from the ground up to **strictly comply with Render's 512 MB Free Tier** without hitting Out-of-Memory (OOM) errors or paying for background cron workers.

---

## ✨ Features

- **InboxCura Workspace:** Integrated Dashboard (Unread, Today, Urgent, Awaiting Reply), Gmail-style Inbox (Primary, Social, Promotions tabs, Reading view with AI summary and inline reply), and an interactive **AI Assistant**.
- **Gemini 3.5 Flash Powered Assistant:** Ask questions directly about your emails or request draft replies with live responses from Gemini 3.5 Flash.
- **Executive Daily Briefing:** Scans incoming emails from the last 24 hours, categorizes them, extracts urgent action items, and compiles an executive markdown summary.
- **TiDB Serverless Deduplication:** External persistent MySQL-compatible storage (5GB free forever) stores processed message IDs so you never summarize or pay LLM tokens for the same email twice.
- **Dual Delivery:** Reads right inside your web dashboard AND automatically sends a clean HTML/Markdown email digest to your inbox.
- **Render Free Tier Optimized:** 
  - Entire server footprint is only **~50–60 MB RAM** (leaves >85% free headroom on Render's 512 MB limit).
  - Handles Render cold starts (~45–50s wake-up) seamlessly via external cron pinging.
  - Zero background server costs: keeps Render sleeping 23.5 hours/day.

---

## 🏗️ Architecture

```
[ Gmail API ] ──(Fetch 24h emails)──┐
                                    ▼
[ Render Web Service ] ──(Deduplicate via TiDB)──> [ TiDB Serverless ] (5GB Free)
  (FastAPI ~55MB RAM)               │
                                    ▼
                          [ Gemini 3.5 Flash ] (Free Tier)
                                    │
               ┌────────────────────┴────────────────────┐
               ▼                                         ▼
      [ Save to TiDB ]                         [ Dispatch Reports ]
  (History & Action items)              - Email to your inbox
                                        - Rendered on Web UI
```

---

## 🚀 Quick Start (Local Setup)

### 1. Clone & Install Dependencies
```bash
git clone <your-repo-url>
cd email_summarise

# Create virtual environment (recommended)
python -m venv venv
venv\Scripts\activate       # Windows
# source venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)
Copy the template:
```bash
copy .env.example .env     # Windows
# cp .env.example .env     # macOS/Linux
```

Fill in the variables:
1. **TiDB Cloud (Free 5GB):**
   - Sign up at [tidbcloud.com](https://tidbcloud.com/) (No credit card needed).
   - Create a free **Serverless Cluster**.
   - Click **Connect** -> copy `TIDB_HOST`, `TIDB_USER`, and `TIDB_PASSWORD`.
2. **Google Gemini API Key:**
   - Get a free key at [aistudio.google.com](https://aistudio.google.com/).
3. **Gmail OAuth Credentials:**
   - In [Google Cloud Console](https://console.cloud.google.com/):
     - Enable the **Gmail API**.
     - Go to **APIs & Services** -> **OAuth consent screen** (User Type: External, add your email as a Test User).
     - Go to **Credentials** -> **Create Credentials** -> **OAuth client ID** (Application type: *Desktop App*).
     - Copy the **Client ID** and **Client Secret** into your `.env`.
   - Run the included helper script to generate your refresh token in 10 seconds:
     ```bash
     python setup_oauth.py
     ```
     *(A browser tab will open. Log into your Gmail account, grant permission, and the script will output your `GMAIL_REFRESH_TOKEN`).*

### 3. Run Locally
```bash
uvicorn app.main:app --reload --port 8000
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser to view your executive dashboard!

---

## 🌐 Deploying to Render (100% Free Tier)

1. Push your repository to **GitHub** (Private repository recommended).
2. Go to [dashboard.render.com](https://dashboard.render.com/) -> **New +** -> **Web Service**.
3. Connect your repository and configure:
   - **Environment:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** `Free` (512 MB RAM)
4. Under **Environment Variables**, add the keys from your `.env`:
   - `TIDB_HOST`, `TIDB_PORT`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DATABASE`
   - `GEMINI_API_KEY`
   - `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`
   - `MY_EMAIL`
   - `CRON_SECRET` *(generate any random 32-character string)*
5. Click **Create Web Service**. Your service will deploy in ~2 minutes!

---

## ⏰ Automated End-of-Day Cron (Zero Cost)

Since Render Cron Jobs are a paid feature, we use **[cron-job.org](https://cron-job.org)** (100% free) to wake up Render and trigger the daily summary:

1. Create a free account on [cron-job.org](https://cron-job.org/).
2. Create a new Cronjob:
   - **Title:** `Daily Gmail Executive Summary`
   - **URL:** `https://your-service-name.onrender.com/api/cron-trigger`
   - **Execution Schedule:** Every day at your desired evening time (e.g., `19:00` / 7:00 PM).
   - **HTTP Method:** `POST`
   - **Headers:**
     - Key: `Authorization`
     - Value: `Bearer <YOUR_CRON_SECRET>`
   - **Advanced Settings -> Request Timeout:** Set to **60 seconds** *(Critical: Gives Render enough time to wake up from free-tier sleep).*

---

## 🔒 Security & Privacy

- **Direct Personal API:** Your emails are only processed through your own Google Cloud OAuth app and Google Gemini.
- **TiDB TLS Encryption:** All database communication is enforced with `ssl_mode: VERIFY_IDENTITY`.
- **Zero Third-Party Middlemen:** No external email services, tracking pixels, or intermediate servers.
