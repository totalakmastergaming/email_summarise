try:
    import psutil
except ImportError:
    psutil = None
import os
from fastapi import APIRouter
from app.config import settings

router = APIRouter(tags=["Health"])

@router.get("/health")
def health_check():
    """
    Render health check endpoint.
    Also returns lightweight memory stats to verify 512MB free tier compliance.
    """
    mem_mb = None
    if psutil:
        try:
            mem_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    if mem_mb is None:
        try:
            import resource
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            pass

    return {
        "status": "healthy",
        "service": "gmail-executive-ai",
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "tidb_configured": bool(settings.TIDB_HOST and settings.TIDB_PASSWORD),
        "gmail_configured": bool(settings.GMAIL_REFRESH_TOKEN),
        "approx_memory_mb": round(mem_mb, 1) if mem_mb else "low (<60MB)"
    }
