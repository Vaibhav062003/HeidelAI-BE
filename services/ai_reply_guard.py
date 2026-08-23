from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


_LEGACY_FAILURE_REPLIES = {
    "our sales team is reviewing this request and will get back to you shortly",
    "our sales team is reviewing this price and will get back to you shortly",
    "i apologize, but i'm having trouble generating a response right now. please try again later",
    "i apologize, but i'm taking longer than usual to respond. please try again in a moment",
    "i apologize, but i'm experiencing technical difficulties. please try again later",
    "i apologize, but something went wrong. please try again later",
}


class AIProviderUnavailable(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        upstream_status: int | None = None,
        escalation_required: bool = True,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.upstream_status = upstream_status
        self.escalation_required = escalation_required


def validate_ai_reply(value: object) -> str:
    if value is None or value == "":
        raise AIProviderUnavailable("empty_response")
    if not isinstance(value, str):
        raise AIProviderUnavailable("malformed_response")
    if not value.strip():
        raise AIProviderUnavailable("empty_response")
    reply = value.strip()
    normalized = " ".join(reply.casefold().split())
    if any(failure in normalized for failure in _LEGACY_FAILURE_REPLIES):
        raise AIProviderUnavailable("legacy_failure_text")
    return reply


def log_ai_failure(
    error: AIProviderUnavailable,
    *,
    org_id: str,
    conversation_id: str,
) -> None:
    logger.warning(
        "AI provider unavailable category=%s status=%s org=%s conversation=...%s",
        error.category,
        error.upstream_status,
        org_id,
        conversation_id[-6:],
    )
