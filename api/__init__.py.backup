from fastapi import APIRouter

from . import ig_routes
from . import wa_routes
from . import webhook_routes
from . import websockets_routes
from . import ai_routes
from . import common_routes
from . import integrations
from . import security_routes
from . import clerk_routes
from . import canned_responses_routes
from . import signup_routes
from . import sse_routes
from . import dashboard_routes
from . import automation_routes
from . import google_routes
from . import google_sheets_routes
from . import wa_bot_routes
from . import product_sheets_routes
from . import team_routes

router = APIRouter()

router.include_router(ig_routes.router)
router.include_router(wa_routes.router)
router.include_router(webhook_routes.router)
router.include_router(websockets_routes.router)
router.include_router(ai_routes.router)
router.include_router(common_routes.router)
router.include_router(integrations.router)
router.include_router(security_routes.router)
router.include_router(clerk_routes.router)
router.include_router(canned_responses_routes.router)
router.include_router(signup_routes.router)
router.include_router(sse_routes.router)
router.include_router(automation_routes.router)
router.include_router(google_routes.router)
router.include_router(google_sheets_routes.router)
router.include_router(dashboard_routes.router)
router.include_router(wa_bot_routes.router)
router.include_router(product_sheets_routes.router)
router.include_router(team_routes.router)
