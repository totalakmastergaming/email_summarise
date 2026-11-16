import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from app.database import init_db, get_system_config
from app.routes.api import router as api_router, cron_router as api_cron_router
from app.routes.health import router as health_router
from app.services.pipeline_service import execute_consolidated_pipeline

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("inboxcura-ai")

# Scheduler state
scheduler_last_run_date = None

async def background_scheduler():
    """Lightweight in-app automated scheduler running in background."""
    global scheduler_last_run_date
    logger.info("InboxCura automated summary scheduler initialized.")
    while True:
        try:
            await asyncio.sleep(45)
            schedule_cfg = get_system_config("summary_schedule")
            if not schedule_cfg or not schedule_cfg.get("enabled"):
                continue

            target_time = schedule_cfg.get("time", "18:00")  # HH:MM
            tz_str = schedule_cfg.get("timezone", "Asia/Kolkata")

            try:
                tz = ZoneInfo(tz_str)
            except Exception:
                tz = ZoneInfo("UTC")

            now_tz = datetime.now(tz)
            current_hhmm = now_tz.strftime("%H:%M")
            current_date_str = now_tz.strftime("%Y-%m-%d")

            if current_hhmm == target_time and scheduler_last_run_date != current_date_str:
                logger.info(f"Triggering scheduled summary at {current_hhmm} ({tz_str}) for {current_date_str}...")
                scheduler_last_run_date = current_date_str
                
                # Execute pipeline safely in threadpool executor without blocking event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, execute_consolidated_pipeline)

        except asyncio.CancelledError:
            logger.info("Scheduler task stopped.")
            break
        except Exception as e:
            logger.error(f"Scheduler loop exception: {e}")

def _init_db_with_retries():
    """Initialize the DB in a background thread, retrying a cold database.

    Runs off the event loop so a stalled first connection can never block
    startup (see lifespan below). Retries 4 times, 15s apart: a TiDB
    Serverless cluster that scaled to zero needs time to resume, and the
    first attempt can fail while it wakes up.
    """
    for attempt in range(1, 5):
        try:
            if init_db():
                logger.info("Database initialized.")
                return
        except Exception as e:
            logger.warning(f"Database init attempt {attempt}/4 raised: {e}")
        if attempt < 4:
            logger.warning(f"Database init attempt {attempt}/4 failed; retrying in 15s...")
            time.sleep(15)
    logger.error("Database init failed after 4 attempts; API routes needing the DB will fail until it recovers.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up InboxCura AI service...")
    # NEVER block startup on the database. init_db() used to run here
    # synchronously: when TiDB was cold, the first connection stalled with
    # no error and no log, uvicorn never printed "Application startup
    # complete", and Render looped on "No open ports detected" — killing
    # three deploys in a row. Now the app binds its port and answers
    # /health immediately; the DB initializes in a background thread and
    # retries on its own. API calls needing the DB fail gracefully until
    # it is up.
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _init_db_with_retries)
    scheduler_task = asyncio.create_task(background_scheduler())
    yield
    logger.info("Shutting down...")
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="InboxCura AI",
    description="InboxCura Workspace powered by Gemini 3.5 Flash and TiDB Serverless.",
    version="2.0.0",
    lifespan=lifespan
)

# NOTE: no CORS middleware on purpose. The UI is served from this same
# app (same origin), so browsers do not need CORS to call /api. The old
# `allow_origins=["*"]` + `allow_credentials=True` combo let ANY website
# the user visited call this API from their browser — and the API has no
# authentication (see NOTES-backend.md) — so any site could read mail,
# delete API keys, or trigger summaries. If a separate frontend origin is
# ever needed, re-add CORSMiddleware with that ONE explicit origin and
# real auth first; never "*" again.

# HTTP Cache-Control Optimization Middleware
@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Static assets: revalidate on every load (no-cache), never a blind
    # max-age window. HOTFIX 2026-11-16 (round 3): max-age=3600 let the
    # browser serve a stale app.js for an hour after a deploy, so the
    # Urgent-card text fix shipped in round 2 never reached the live
    # browser even though the deploy itself was correct. Starlette
    # answers conditional requests with 304, so unchanged files still
    # cost almost nothing.
    if path.startswith(("/css", "/js", "/assets", "/static")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    # API endpoints: Never browser-cache dynamic data
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# Include API Routes (cron webhook is a separate router: CRON_SECRET auth only)
app.include_router(health_router)
app.include_router(api_router)
app.include_router(api_cron_router)

# Mount Static Directories for InboxCura Frontend
app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")
app.mount("/assets", StaticFiles(directory="static/assets"), name="assets")
app.mount("/static", StaticFiles(directory="static"), name="static")

def _asset_version():
    """Cache-busting version for the dashboard's static assets.

    Render sets RENDER_GIT_COMMIT at deploy time, so every deploy is a
    new asset URL and even a warm browser cache — e.g. one cached under
    the old max-age=3600 headers — is bypassed (HOTFIX 2026-11-16,
    round 3). Locally, fall back to the app.js mtime so edits are
    picked up without a restart.
    """
    commit = os.environ.get("RENDER_GIT_COMMIT", "")
    if commit:
        return commit[:7]
    try:
        return str(int(Path("static/js/app.js").stat().st_mtime))
    except OSError:
        return "dev"


@app.get("/")
def serve_dashboard():
    """Serves the InboxCura web UI, with versioned asset URLs."""
    html = Path("static/index.html").read_text(encoding="utf-8")
    v = _asset_version()
    html = (html.replace('href="css/style.css"', 'href="css/style.css?v=' + v + '"')
                .replace('src="js/app.js"', 'src="js/app.js?v=' + v + '"')
                .replace('src="assets/logo-lockup.png"', 'src="assets/logo-lockup.png?v=' + v + '"')
                .replace("src='assets/logo-lockup.svg'", "src='assets/logo-lockup.svg?v=" + v + "'"))
    return HTMLResponse(html)
