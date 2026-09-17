"""Shared boundary types for purchase preparation and agent corrections."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CatalogueMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=240)
    name: str = Field(default="", max_length=500)
    sku: str = Field(default="", max_length=240)


class PurchaseItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    line_id: str = Field(min_length=1, max_length=100)
    request: str = Field(min_length=1, max_length=500)
    quantity: int | None = Field(default=None, ge=1, le=9999, strict=True)
    product_id: str | None = Field(default=None, max_length=240)
    source_message_id: str = Field(default="", max_length=240)
    evidence: str = Field(default="", max_length=2000)
    quantity_evidence: str = Field(default="", max_length=2000)
    agent_corrected: bool = False
    matches: list[CatalogueMatch] = Field(default_factory=list, max_length=3)


class PurchaseCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    items: list[PurchaseItem] = Field(max_length=50)
    purchase_path: Literal["quote", "retail"] = "quote"


class PreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)


class NewPurchaseRequest(PreparationRequest):
    request_id: str = Field(min_length=1, max_length=100)


class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    b2b_pricing_enabled: bool | None = None


class WorkSummary(BaseModel):
    attention: Literal["needs_action", "blocked", "follow_up_due", "waiting", "completed"]
    action: str
    reason: str = ""
    owner_id: str | None = None
    owner_name: str | None = None
    due_at: datetime | None = None
