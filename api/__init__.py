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