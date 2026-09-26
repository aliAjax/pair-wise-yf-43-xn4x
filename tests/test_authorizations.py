import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class AuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.authorizer = Actor("auth-1", "authorizer")
        self.analyst = Actor("lab-1", "analyst")
        self.instrument = self._make_instrument("A-1", due_at="2099-01-01")
        self.method_v1 = self._make_method("Assay-A", "v1")
        self.method_v2 = self._make_method("Assay-A", "v2")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_instrument(self, serial, due_at):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": serial}
        )
        self.service.transition(
            self.admin, instrument["id"], "send_calibration", {}
        )
        return self.service.transition(
            self.admin,
            instrument["id"],
            "calibrate",
            {"due_at": due_at, "passed": True},
        )

    def _make_calibration(self, result="passed", performed_at="2026-01-02"):
        calibration = self.service.create(
            self.admin,
            "calibration",
            {
                "instrument_id": self.instrument["id"],
                "requested_at": "2026-01-01",
            },
        )
        self.service.transition(
            self.admin,
            calibration["id"],
            "perform",
            {
                "result": result,
                "performed_at": performed_at,
                "uncertainty": 0.01,
                "due_at": "2099-01-01",
            },
        )
        if result == "passed":
            self.service.transition(
                self.admin,
                calibration["id"],
                "approve",
                {"authorized_by": "QA-1"},
            )
        return calibration

    def _make_method(self, name, version):
        method = self.service.create(
            self.admin, "method", {"name": name, "version": version}
        )
        return self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 100]}, "instrument_ids": [self.instrument["id"]]},
        )

    def _register_clause(
        self,
        clause_no,
        method_id,
        lower,
        upper,
        uncertainty_limit,
        expires_at="2099-12-31",
        range_name=None,
        effective_at=None,
        actor=None,
    ):
        data = {
            "clause_no": clause_no,
            "instrument_id": self.instrument["id"],
            "method_id": method_id,
            "range_name": range_name or ("range-%s-%s" % (lower, upper)),
            "lower_limit": lower,
            "upper_limit": upper,
            "uncertainty_limit": uncertainty_limit,
            "expires_at": expires_at,
        }
        if effective_at:
            data["effective_at"] = effective_at
        return self.service.create(actor or self.authorizer, "authorization", data)

    def _submit_result(self, value, uncertainty, used_at="2026-09-26", method_id=None):
        result = self.service.create(
            self.analyst,
            "result",
            {"sample_id": "S-" + str(value), "measurement": "raw"},
        )
        return result, self.service.transition(
            self.analyst,
            result["id"],
            "release",
            {
                "instrument_id": self.instrument["id"],
                "method_id": method_id or self.method_v1["id"],
                "value": value,
                "expanded_uncertainty": uncertainty,
                "used_at": used_at,
            },
        )

    def test_release_saves_clause_no_and_version(self):
        self._make_calibration()
        self._register_clause("C-V1-LOW", self.method_v1["id"], 0, 10, 0.5)
        _, released = self._submit_result(4.2, 0.1)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["clause_no"], "C-V1-LOW")
        self.assertEqual(released["data"]["method_version"], "v1")
        self.assertEqual(released["data"]["released_by"], "lab-1")

    def test_method_revision_requires_new_clause(self):
        self._make_calibration()
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        # v2 换版后不能再套用 v1 的通用范围。
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1, method_id=self.method_v2["id"])
        self.assertIn("授权条款", str(caught.exception))
        self._register_clause("C-V2", self.method_v2["id"], 0, 50, 0.3)
        _, released = self._submit_result(5.0, 0.1, method_id=self.method_v2["id"])
        self.assertEqual(released["data"]["clause_no"], "C-V2")
        self.assertEqual(released["data"]["method_version"], "v2")

    def test_value_outside_range_is_rejected(self):
        self._make_calibration()
        self._register_clause("C-V1-LOW", self.method_v1["id"], 0, 10, 0.5)
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(12.0, 0.1)
        self.assertIn("条款越界", str(caught.exception))

    def test_uncertainty_over_limit_is_rejected(self):
        self._make_calibration()
        self._register_clause("C-V1-LOW", self.method_v1["id"], 0, 10, 0.2)
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.4)
        self.assertIn("不确定度上限", str(caught.exception))

    def test_expired_clause_is_rejected(self):
        self._make_calibration()
        self._register_clause(
            "C-OLD", self.method_v1["id"], 0, 10, 0.5, expires_at="2026-06-30"
        )
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1, used_at="2026-09-26")
        self.assertIn("无有效授权条款", str(caught.exception))

    def test_failed_calibration_is_rejected(self):
        self._make_calibration(result="passed", performed_at="2026-01-02")
        self._make_calibration(result="failed", performed_at="2026-05-01")
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1)
        self.assertIn("仪器校准不合格", str(caught.exception))

    def test_expired_calibration_certificate_is_rejected(self):
        expired_instrument = self._make_instrument("A-OLD", due_at="2025-01-01")
        self._register_clause(
            "C-EXP", self.method_v1["id"], 0, 10, 0.5
        )
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-2", "measurement": "raw"}
        )
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.analyst,
                result["id"],
                "release",
                {
                    "instrument_id": expired_instrument["id"],
                    "method_id": self.method_v1["id"],
                    "value": 5.0,
                    "expanded_uncertainty": 0.1,
                    "used_at": "2026-09-26",
                },
            )
        self.assertIn("仪器校准不合格", str(caught.exception))

    def test_quarantined_instrument_is_rejected(self):
        self._make_calibration()
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        self.service.transition(
            self.admin,
            self.instrument["id"],
            "quarantine",
            {"reason": "out of order"},
        )
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1)
        self.assertIn("仪器校准不合格", str(caught.exception))

    def test_revoked_method_is_rejected(self):
        self._make_calibration()
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        self.service.transition(
            self.admin, self.method_v1["id"], "revoke_method", {"reason": "replaced"}
        )
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1)
        self.assertIn("方法已撤回", str(caught.exception))

    def test_withdrawn_clause_is_rejected(self):
        self._make_calibration()
        clause = self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        self.service.transition(
            self.authorizer, clause["id"], "withdraw", {"reason": "obsolete"}
        )
        with self.assertRaises(ValidationError) as caught:
            self._submit_result(5.0, 0.1)
        self.assertIn("授权条款", str(caught.exception))

    def test_boundary_values_are_accepted(self):
        self._make_calibration()
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        for boundary in (0, 10):
            _, released = self._submit_result(boundary, 0.5)
            self.assertEqual(released["status"], "released")

    def test_rejected_result_stays_pending_and_can_resubmit(self):
        self._make_calibration()
        result = self.service.create(
            self.analyst, "result", {"sample_id": "S-9", "measurement": "raw"}
        )
        payload = {
            "instrument_id": self.instrument["id"],
            "method_id": self.method_v1["id"],
            "value": 5.0,
            "expanded_uncertainty": 0.1,
            "used_at": "2026-09-26",
        }
        with self.assertRaises(ValidationError):
            self.service.transition(self.analyst, result["id"], "release", payload)
        self.assertEqual(self.service.get(result["id"])["status"], "pending")

        records = self.service.audit_log(result["id"])
        self.assertEqual(records[-1]["action"], "release_rejected")
        self.assertIn("授权条款", records[-1]["detail"]["reason"])
        self.assertEqual(records[-1]["detail"]["submission"]["value"], 5.0)

        # 补充登记条款后再次提交，原结果仍可放行。
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        released = self.service.transition(
            self.analyst, result["id"], "release", payload
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["clause_no"], "C-V1")

    def test_overlapping_ranges_prefer_narrowest(self):
        self._make_calibration()
        self._register_clause("C-WIDE", self.method_v1["id"], 0, 10, 0.5)
        self._register_clause("C-NARROW", self.method_v1["id"], 2, 8, 0.2)
        _, released = self._submit_result(5.0, 0.15)
        self.assertEqual(released["data"]["clause_no"], "C-NARROW")
        # 窄量程不确定度不满足时，落到仍覆盖的宽量程。
        _, released_wide = self._submit_result(5.1, 0.3)
        self.assertEqual(released_wide["data"]["clause_no"], "C-WIDE")

    def test_clause_requires_validated_method(self):
        draft = self.service.create(
            self.admin, "method", {"name": "Draft-X", "version": "d1"}
        )
        with self.assertRaises(ValidationError):
            self._register_clause("C-DRAFT", draft["id"], 0, 10, 0.5)

    def test_duplicate_clause_no_conflicts(self):
        self._register_clause("C-DUP", self.method_v1["id"], 0, 10, 0.5)
        with self.assertRaises(ConflictError):
            self._register_clause("C-DUP", self.method_v2["id"], 0, 50, 0.5)

    def test_invalid_limits_are_rejected(self):
        with self.assertRaises(ValidationError):
            self._register_clause("C-BAD", self.method_v1["id"], 10, 10, 0.5)
        with self.assertRaises(ValidationError):
            self._register_clause("C-NEG", self.method_v1["id"], 0, 10, 0)

    def test_effective_at_gates_usage_date(self):
        self._make_calibration()
        self._register_clause(
            "C-FUTURE",
            self.method_v1["id"],
            0,
            10,
            0.5,
            effective_at="2026-10-01",
        )
        with self.assertRaises(ValidationError):
            self._submit_result(5.0, 0.1, used_at="2026-09-26")
        _, released = self._submit_result(5.0, 0.1, used_at="2026-10-02")
        self.assertEqual(released["status"], "released")

    def test_analyst_cannot_register_clause_but_can_release(self):
        with self.assertRaises(PermissionDenied):
            self._register_clause(
                "C-ROLE", self.method_v1["id"], 0, 10, 0.5, actor=self.analyst
            )
        self._make_calibration()
        self._register_clause("C-V1", self.method_v1["id"], 0, 10, 0.5)
        _, released = self._submit_result(5.0, 0.1)
        self.assertEqual(released["status"], "released")


if __name__ == "__main__":
    unittest.main()
