from fastapi import APIRouter, Request

from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.sales_case_service import get_active_sales_case
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


router = APIRouter(prefix="/api/sales-cases", tags=["Sales cases"])


@router.get("/conversations/{conversation_id}")
@limiter.limit("60/minute")
async def conversation_sales_case(
    request: Request,
    conversation_id: str,
    user: CurrentUser,
):
    conversation = await require_conversation_access(
        user.org_id, conversation_id, user.user_id, user.role,
    )
    case = await get_active_sales_case(user.org_id, conversation_id)
    workflow_enabled = await workflow.enabled(user.org_id)
    if workflow_enabled and case:
        case = await workflow.case_view({**case, "workflow_version": 2}, conversation)
    if case:
        case.pop("org_id", None)
        case.pop("processed_events", None)
    return {"case": serialize_mongo(case) if case else None, **({"workflow_enabled": True} if workflow_enabled else {})}


from auth.auth_utils import admin_required
from schemas.sales_workflow import PurchaseCorrection, PreparationRequest, NewPurchaseRequest, WorkflowSettings
from services import sales_workflow_service as workflow


@router.get("/settings")
async def workflow_settings(user: CurrentUser):
    from services.b2b_pricing_service import enabled as b2b_enabled
    return {"enabled": await workflow.enabled(user.org_id), "b2b_pricing_enabled": await b2b_enabled(user.org_id)}


@router.put("/settings")
async def update_workflow_settings(payload: WorkflowSettings, user: CurrentUser):
    await admin_required(user)
    from services.entitlement_service import require_feature
    require_feature(user.org_id, "AI", strict=True)
    fields = {"sales_workflow_v2_enabled": payload.enabled}
    if payload.b2b_pricing_enabled is not None:
        fields["b2b_pricing_enabled"] = payload.b2b_pricing_enabled
        if payload.b2b_pricing_enabled:
            from services.b2b_pricing_service import ensure_indexes
            await ensure_indexes(user.org_id)
    await workflow._database().organizations.update_one({"org_id": user.org_id}, {"$set": fields})
    return await workflow_settings(user)


async def purchase_access(user, conversation_id):
    await workflow.require_enabled(user.org_id)
    return await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)


@router.patch("/conversations/{conversation_id}/requirements")
@limiter.limit("30/minute")
async def correct_purchase(request: Request, conversation_id: str, payload: PurchaseCorrection, user: CurrentUser):
    conversation = await purchase_access(user, conversation_id)
    if payload.case_id == "new" and await get_active_sales_case(user.org_id, conversation_id):
        from fastapi import HTTPException
        raise HTTPException(409, "A purchase has started. Refresh before applying these edits")
    case = await workflow.ensure_case(user.org_id, conversation_id)
    from services.b2b_pricing_service import enabled as b2b_enabled
    if payload.purchase_path == "retail" and await b2b_enabled(user.org_id):
        from fastapi import HTTPException
        raise HTTPException(422, "B2B purchases use quotations and billing")
    if payload.case_id != "new" and case["case_id"] != payload.case_id:
        from fastapi import HTTPException
        raise HTTPException(409, "A new purchase has started. Refresh before applying these edits")
    if case.get("billing_status") in workflow.TERMINAL:
        from fastapi import HTTPException
        raise HTTPException(409, "Start a new purchase before changing a closed purchase")
    items = await workflow.verify_matches(user.org_id, [item.model_dump() for item in payload.items], manual=True)
    case = await workflow.save_items(case, items, payload.expected_version, actor=user.user_id, purchase_path=payload.purchase_path)
    await workflow.cancel_followups(user.org_id, conversation_id)
    return {"case": serialize_mongo(await workflow.case_view(case, conversation))}


@router.post("/conversations/{conversation_id}/prepare")
@limiter.limit("10/minute")
async def prepare_purchase(request: Request, conversation_id: str, payload: PreparationRequest, user: CurrentUser):
    conversation = await purchase_access(user, conversation_id)
    case = await workflow.prepare_purchase(user.org_id, conversation_id, payload.expected_version, actor=user.user_id, expected_case_id=payload.case_id)
    # Preparing in the inbox never sends a customer message.
    return {"case": serialize_mongo(await workflow.case_view(case, conversation))}


@router.post("/conversations/{conversation_id}/new")
@limiter.limit("10/minute")
async def start_new_purchase(request: Request, conversation_id: str, payload: NewPurchaseRequest, user: CurrentUser):
    conversation = await purchase_access(user, conversation_id)
    case = await workflow.new_purchase(user.org_id, conversation_id, payload.expected_version, payload.request_id, expected_case_id=payload.case_id)
    return {"case": serialize_mongo(await workflow.case_view(case, conversation))}
