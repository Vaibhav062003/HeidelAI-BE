"""Validate actual Pydantic schemas without importing the agent/runtime credentials."""
import ast
from pathlib import Path
from typing import Literal, Optional
import unittest

try:
    from pydantic import BaseModel, Field, field_validator, ValidationError
except ImportError:
    BaseModel = None


@unittest.skipIf(BaseModel is None, "Pydantic runtime dependency is not installed")
class ProductRequestSchemaTest(unittest.TestCase):
    def setUp(self):
        names = {"ProductRequestExtraction", "PurchaseIntentResult", "ProductVerification", "ProductVerificationBatch"}
        nodes = [n for n in ast.parse(Path("agents/intent_agent/agent.py").read_text()).body if isinstance(n, ast.ClassDef) and n.name in names]
        self.scope = dict(BaseModel=BaseModel, Field=Field, field_validator=field_validator,
                          ValidationError=ValidationError, Optional=Optional, Literal=Literal, __name__=__name__)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "product_schemas", "exec"), self.scope)
        self.valid = {"product_name": "Laptop", "source_message_id": "m1", "evidence": "I want Laptop",
                      "confidence": "high", "entity_type": "product", "action": "enquiry", "notification_action": "none"}

    def test_malformed_sibling_does_not_discard_valid_product_or_purchase_followup(self):
        result = self.scope["PurchaseIntentResult"].model_validate({"has_intent": True, "product_name": "Laptop",
            "tentative_date": "2026-10-11T10:00:00+05:30", "confidence": "high", "reasoning": "Tomorrow",
            "product_requests": [self.valid, {**self.valid, "confidence": "invented"}, "bad", None]})
        self.assertTrue(result.has_intent)
        self.assertEqual(len(result.product_requests), 1)
        self.assertEqual(result.product_requests[0].product_name, "Laptop")

    def test_unresolved_and_independent_revocation_are_valid(self):
        result = self.scope["ProductRequestExtraction"].model_validate({**self.valid,
            "product_name": None, "entity_type": "unresolved", "action": "withdrawal", "notification_action": "revoke"})
        self.assertIsNone(result.product_name)
        self.assertEqual(result.notification_action, "revoke")

    def test_invalid_verification_sibling_is_omitted_for_review_fallback(self):
        verdict = {"candidate_id": "valid", "verification_status": "confirmed", "product_name": "Laptop",
                   "entity_type": "product", "action": "enquiry", "notification_action": "none", "reason": "Supported"}
        result = self.scope["ProductVerificationBatch"].model_validate({"decisions": [verdict, {"candidate_id": "invalid"}]})
        self.assertEqual([v.candidate_id for v in result.decisions], ["valid"])

    def test_schemas_are_serializable_and_observations_are_bounded(self):
        for name in ("PurchaseIntentResult", "ProductVerificationBatch"):
            self.assertIn("properties", self.scope[name].model_json_schema())
        result = self.scope["PurchaseIntentResult"].model_validate({"has_intent": False, "confidence": "high",
            "reasoning": "No time", "product_requests": [self.valid] * 25})
        self.assertEqual(len(result.product_requests), 20)


@unittest.skipIf(BaseModel is None, "Pydantic runtime dependency is not installed")
class ProductRequestAPISchemaTest(unittest.TestCase):
    def setUp(self):
        names = {"Review", "Audience", "SendAudience"}
        nodes = [n for n in ast.parse(Path("api/product_request_routes.py").read_text()).body if isinstance(n, ast.ClassDef) and n.name in names]
        self.scope = dict(BaseModel=BaseModel, Field=Field, Literal=Literal, __name__=__name__)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "review_schemas", "exec"), self.scope)

    def test_review_and_send_require_revision_contract(self):
        with self.assertRaises(ValidationError):
            self.scope["Review"].model_validate({"product_id": "p1"})
        with self.assertRaises(ValidationError):
            self.scope["SendAudience"].model_validate({"request_ids": ["r1"]})
        result = self.scope["Review"].model_validate({"expected_revision": 0, "verification_status": "confirmed", "product_name": "Unknown model"})
        self.assertIsNone(result.product_id)
        self.assertEqual(result.expected_revision, 0)
        with self.assertRaises(ValidationError):
            self.scope["SendAudience"].model_validate({"request_ids": ["r1"], "revisions": {str(i): 0 for i in range(101)}})


if __name__ == "__main__": unittest.main()
