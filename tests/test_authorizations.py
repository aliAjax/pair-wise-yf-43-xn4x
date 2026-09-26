import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ReleaseRejected, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
ANALYST = Actor("analyst-1", "analyst")
AUTHORIZER = Actor("auth-1", "authorizer")


class AuthorizationReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.instrument = self._instrument()
        self._passed_calibration(due_at="2099-01-01", performed_at="2026-01-02")
        self.method = self._method("v2")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        return self.service.create(
            ADMIN, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )["id"]

    def _passed_calibration(self, due_at="2099-01-01", performed_at="2026-01-02",
                            result="passed"):
        calibration = self.service.create(
            ADMIN,
            "calibration",
            {"instrument_id": self.instrument, "requested_at": "2026-01-01"},
        )["id"]
        self.service.transition(
            ADMIN,
            calibration,
            "perform",
            {"result": result, "performed_at": performed_at,
             "uncertainty": 0.01, "due_at": due_at},
        )
        return calibration

    def _method(self, version="v2"):
        method = self.service.create(
            ADMIN, "method", {"name": "Assay-A", "version": version}
        )["id"]
        self.service.transition(
            AUTHORIZER,
            method,
            "validate_method",
            {"parameters": {"ranges": [[0, 10]]}},
        )
        return method

    def _clause(self, lower=0, upper=10, max_uncertainty=0.1,
                valid_until="2099-01-01", clause_no="M-A1-01",
                clause_version="v2", instrument=None, method=None):
        return self.service.create(
            AUTHORIZER,
            "authorization",
            {
                "method_id": method or self.method,
                "instrument_id": instrument or self.instrument,
                "clause_no": clause_no,
                "clause_version": clause_version,
                "range_name": "%s-%s" % (lower, upper),
                "lower_limit": lower,
                "upper_limit": upper,
                "max_uncertainty": max_uncertainty,
                "valid_until": valid_until,
            },
        )["id"]

    def _result(self):
        return self.service.create(
            ANALYST, "result", {"sample_id": "S-1", "measurement": "initial"}
        )["id"]

    def _release(self, result, value=4.2, uncertainty=0.05, used_at="2026-09-26"):
        return self.service.transition(
            ANALYST,
            result,
            "release",
            {
                "instrument_id": self.instrument,
                "method_id": self.method,
                "value": value,
                "expanded_uncertainty": uncertainty,
                "used_at": used_at,
            },
        )

    def test_release_records_matched_clause_and_versions(self):
        clause = self._clause()
        result = self._result()
        released = self._release(result)
        self.assertEqual(released["status"], "released")
        data = released["data"]
        self.assertEqual(data["authorization_id"], clause)
        self.assertEqual(data["clause_no"], "M-A1-01")
        self.assertEqual(data["clause_version"], "v2")
        self.assertEqual(data["method_version"], "v2")

    def test_narrowest_matching_range_wins(self):
        self._clause(0, 10, clause_no="WIDE")
        narrow = self._clause(4, 6, clause_no="NARROW")
        result = self._result()
        released = self._release(result, value=5.0)
        self.assertEqual(released["data"]["authorization_id"], narrow)
        self.assertEqual(released["data"]["clause_no"], "NARROW")

    def test_value_out_of_range_leaves_result_pending(self):
        self._clause(0, 10)
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result, value=11.5)
        self.assertEqual(caught.exception.reason, "value_out_of_range")
        self.assertEqual(self.service.get(result)["status"], "pending")

        # After fixing the condition the same result can be resubmitted.
        self._clause(10, 20, clause_no="M-A1-02")
        released = self._release(result, value=11.5)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["clause_no"], "M-A1-02")

    def test_uncertainty_over_ceiling_rejected(self):
        self._clause(0, 10, max_uncertainty=0.05)
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result, uncertainty=0.08)
        self.assertEqual(caught.exception.reason, "uncertainty_exceeded")
        self.assertEqual(self.service.get(result)["status"], "pending")

    def test_expired_clause_rejected(self):
        self._clause(valid_until="2026-01-01")
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result, used_at="2026-09-26")
        self.assertEqual(caught.exception.reason, "clause_expired")
        self.assertEqual(self.service.get(result)["status"], "pending")

    def test_clause_valid_on_last_day_passes(self):
        self._clause(valid_until="2026-09-26")
        result = self._result()
        released = self._release(result, used_at="2026-09-26")
        self.assertEqual(released["status"], "released")

    def test_revoked_clause_rejected(self):
        clause = self._clause()
        self.service.transition(
            AUTHORIZER, clause, "revoke", {"reason": "method group reissued"}
        )
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result)
        self.assertEqual(caught.exception.reason, "clause_missing")
        self.assertEqual(self.service.get(clause)["status"], "revoked")

    def test_failed_calibration_rejected(self):
        self._passed_calibration(due_at="2099-12-31", performed_at="2026-05-01",
                                 result="failed")
        self._clause()
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result)
        self.assertEqual(caught.exception.reason, "calibration_failed")
        self.assertEqual(self.service.get(result)["status"], "pending")

    def test_expired_calibration_rejected(self):
        self._passed_calibration(due_at="2026-01-01", performed_at="2026-05-01")
        self._clause()
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result, used_at="2026-09-26")
        self.assertEqual(caught.exception.reason, "calibration_expired")

    def test_revoked_method_rejected(self):
        self._clause()
        self.service.transition(
            AUTHORIZER, self.method, "revoke_method", {"reason": "replaced by v3"}
        )
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result)
        self.assertEqual(caught.exception.reason, "method_revoked")
        self.assertEqual(self.service.get(result)["status"], "pending")

    def test_other_instrument_clause_does_not_apply(self):
        other = self.service.create(
            ADMIN, "instrument", {"name": "Other", "serial": "B-2"}
        )["id"]
        self._clause(instrument=other)
        result = self._result()
        with self.assertRaises(ReleaseRejected) as caught:
            self._release(result)
        self.assertEqual(caught.exception.reason, "clause_missing")

    def test_duplicate_active_clause_number_rejected(self):
        self._clause(clause_no="DUP")
        with self.assertRaises(ValidationError):
            self._clause(clause_no="DUP")

    def test_inverted_limits_rejected(self):
        with self.assertRaises(ValidationError):
            self._clause(lower=10, upper=0)

    def test_rejection_is_audited_without_mutating_result(self):
        self._clause(0, 10)
        result = self._result()
        with self.assertRaises(ReleaseRejected):
            self._release(result, value=99)
        records = self.service.audit_log(result)
        rejection = [item for item in records if item["action"] == "release_rejected"]
        self.assertEqual(len(rejection), 1)
        self.assertEqual(rejection[0]["detail"]["reason"], "value_out_of_range")
        self.assertEqual(rejection[0]["to_status"], "pending")


if __name__ == "__main__":
    unittest.main()
