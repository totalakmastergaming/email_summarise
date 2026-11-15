# ⚡ InboxCura — Manage Your Complete Gmail

InboxCura is a **website for managing complete Gmail**: a Gmail-style inbox (Primary, Social, Promotions tabs, reading view, reply/forward, labels, search, filters, starring, archiving, deleting), a dashboard (Unread, Today, Urgent, Awaiting Reply), and an interactive **AI Assistant** — with an **executive AI briefing** (Gemini 3.5 Flash, Groq fallback) as one feature inside it, not the whole product.

Backed by **FastAPI + TiDB Serverless**, built from the ground up to **strictly comply with Render's 512 MB Free Tier** without hitting Out-of-Memory (OOM) errors or paying for background cron workers.

---

## ✨ Features

- **Full Gmail workspace:** Gmail-style Inbox (Primary, Social, Promotions tabs from Gmail's own category labels, reading view with the **full email body**, inline reply and compose that **send via the Gmail API**), real labels (apply/remove Gmail labels), server search, bulk actions (mark-all-read, archive, spam, delete — all synced back to Gmail in one call), starring, archiving, deleting.
- **Dashboard:** Unread, Mail received today, Urgent, Awaiting your reply — all computed from live Gmail state, with an AI brief for today.
- **AI Assistant:** Ask questions directly about your emails or request draft replies, with automatic provider failover (Gemini 3.5 Flash → Groq `openai/gpt-oss-120b`).
- **Executive Daily Briefing:** Scans incoming emails, categorizes them, extracts urgent action items, and compiles a markdown summary. Deduplication in TiDB means you never summarize or pay LLM tokens for the same email twice.
- **Admin token auth:** every `/api` route (except the cron webhook) requires `Authorization: Bearer <ADMIN_TOKEN>`. Enter the token once in the Configuration view; it is kept only in that browser tab. If `ADMIN_TOKEN` is not set on the server, the API fails closed (503).
- **Encrypted API keys at rest:** provider keys in TiDB are encrypted with Fernet (`API_KEY_ENCRYPTION_KEY`); legacy plaintext rows are re-encrypted on next write.
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
   - `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash`)
   - `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`
   - `MY_EMAIL`
   - `CRON_SECRET` *(generate any random 32-character string)*
   - `ADMIN_TOKEN` *(generated for you if you deploy from `render.yaml` — copy it from the Render dashboard into the app's Configuration view)*
   - `API_KEY_ENCRYPTION_KEY` *(generated for you if you deploy from `render.yaml`; encrypts provider keys stored in TiDB)*
5. Click **Create Web Service**. Your service will deploy in ~2 minutes!
6. Open the app, go to **Configuration → Admin Access**, and paste the `ADMIN_TOKEN` from the Render dashboard. Until you do, the dashboard shows a lock notice and no data loads.

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

- **Admin token on every API route:** all `/api` endpoints except `/api/cron-trigger` require `Authorization: Bearer <ADMIN_TOKEN>`. The token is generated in Render (`render.yaml` sets `generateValue: true`) and is never committed. If it is not set, the API fails closed with 503 instead of serving mail to the open internet. The cron webhook keeps its own `CRON_SECRET` with a timing-safe comparison.
- **Encrypted provider keys:** API keys stored in TiDB are Fernet-encrypted (`API_KEY_ENCRYPTION_KEY`). Without that variable, keys are stored as plaintext with a startup warning.
- **No wildcard CORS:** the UI is served same-origin, so no CORS middleware is configured at all.
- **Direct Personal API:** Your emails are only processed through your own Google Cloud OAuth app and Google Gemini.
- **TiDB TLS Encryption:** All database communication is enforced with `ssl_mode: VERIFY_IDENTITY`.
- **Zero Third-Party Middlemen:** No external email services, tracking pixels, or intermediate servers.
