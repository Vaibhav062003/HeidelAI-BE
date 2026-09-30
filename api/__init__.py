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
from . import transfer_routes
from . import push_routes
from . import quote_routes
from . import sales_offer_routes
from . import sales_case_routes
from . import work_team_routes
from . import tally_routes
from . import stock_routes
from . import entitlement_routes
from . import billing_routes
from . import internal_sales_learning_routes
from . import shopify_routes
from . import commerce_growth_routes
from . import pricing_routes
from . import product_request_routes
from . import native_invoice_routes
from . import whatsapp_history_routes

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
router.include_router(transfer_routes.router)
router.include_router(push_routes.router)
router.include_router(quote_routes.router)
router.include_router(sales_offer_routes.router)
router.include_router(sales_case_routes.router)
router.include_router(work_team_routes.router)
router.include_router(tally_routes.router)
router.include_router(stock_routes.router)
router.include_router(entitlement_routes.router)
router.include_router(billing_routes.router)
router.include_router(internal_sales_learning_routes.router)
router.include_router(shopify_routes.router)
router.include_router(commerce_growth_routes.router)
router.include_router(pricing_routes.router)
router.include_router(product_request_routes.router)
router.include_router(native_invoice_routes.router)
router.include_router(whatsapp_history_routes.router)
