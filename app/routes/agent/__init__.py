from fastapi import APIRouter

from .ingest import router as _ingest
from .pair import router as _pair
from .analytics import router as _analytics

router = APIRouter(prefix="/api/agent", tags=["agent"])
router.include_router(_ingest)
router.include_router(_pair)
router.include_router(_analytics)
