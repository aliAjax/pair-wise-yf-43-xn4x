from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _parse_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        raise ValidationError("%s must be an ISO date like 2026-09-26" % field)


def _as_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s must be a number" % field)
    return float(value)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at)[:10] >= str(as_of)[:10]


def _validate_authorization(actor, data, lookup):
    # 授权条款把方法验证拆到“器具 + 量程”粒度，方法换版后需按新版本重新登记。
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("authorization requires an existing instrument")
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        raise ValidationError("authorization requires an existing method")
    if method["status"] != "validated":
        raise ValidationError("authorization requires a validated method")

    lower = _as_number(data.get("lower_limit"), "lower_limit")
    upper = _as_number(data.get("upper_limit"), "upper_limit")
    uncertainty_limit = _as_number(
        data.get("uncertainty_limit"), "uncertainty_limit"
    )
    if lower >= upper:
        raise ValidationError("lower_limit must be below upper_limit")
    if uncertainty_limit <= 0:
        raise ValidationError("uncertainty_limit must be positive")

    expires_at = _parse_date(data.get("expires_at"), "expires_at")
    if data.get("effective_at") is not None:
        effective_at = _parse_date(data.get("effective_at"), "effective_at")
        if effective_at > expires_at:
            raise ValidationError("effective_at must not be after expires_at")

    if _find_one(lookup, "authorization", "clause_no", data.get("clause_no")):
        raise ConflictError("clause_no already exists: " + str(data.get("clause_no")))

    # 固化登记时的方法版本，放行时随结果保存，避免换版后追溯不清。
    data["method_version"] = method["data"].get("version")


def _latest_calibration(calibrations):
    performed = [c for c in calibrations if c["data"].get("performed_at")]
    if not performed:
        return None
    return max(performed, key=lambda c: str(c["data"]["performed_at"])[:10])


def _clause_in_effect(clause, used_on):
    if used_on > _parse_date(clause["data"].get("expires_at"), "expires_at"):
        return False
    effective_at = clause["data"].get("effective_at")
    if effective_at and used_on < _parse_date(effective_at, "effective_at"):
        return False
    return True


def _clause_span(clause):
    data = clause["data"]
    return _as_number(data["upper_limit"], "upper_limit") - _as_number(
        data["lower_limit"], "lower_limit"
    )


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("驳回：仪器不存在，无法放行")
    if instrument["status"] != "active":
        raise ValidationError(
            "驳回：仪器校准不合格或不可用，仪器当前状态为 %s" % instrument["status"]
        )

    latest = _latest_calibration(
        lookup("calibration", "instrument_id", instrument["id"]) or []
    )
    if latest and (
        latest["status"] == "rejected" or latest["data"].get("result") == "failed"
    ):
        raise ValidationError("驳回：仪器校准不合格，最近一次校准未通过")

    used_on = _parse_date(data.get("used_at"), "used_at")
    due_at = instrument["data"].get("due_at")
    if not due_at or not calibration_current(due_at, used_on.isoformat()):
        raise ValidationError(
            "驳回：仪器校准不合格，校准证书在使用日期 %s 已过期"
            % used_on.isoformat()
        )

    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        raise ValidationError("驳回：方法不存在，无法放行")
    if method["status"] == "revoked":
        raise ValidationError("驳回：方法已撤回，禁止放行")
    if method["status"] != "validated":
        raise ValidationError(
            "驳回：方法尚未验证通过，当前状态为 %s" % method["status"]
        )

    value = _as_number(data.get("value"), "value")
    uncertainty = _as_number(
        data.get("expanded_uncertainty"), "expanded_uncertainty"
    )
    if uncertainty < 0:
        raise ValidationError("驳回：扩展不确定度不能为负数")

    clauses = [
        clause
        for clause in (
            lookup("authorization", "instrument_id", instrument["id"]) or []
        )
        if clause["status"] == "active"
        and clause["data"].get("method_id") == method["id"]
    ]
    effective_clauses = [c for c in clauses if _clause_in_effect(c, used_on)]
    if not effective_clauses:
        if clauses:
            raise ValidationError(
                "驳回：该器具与方法在使用日期 %s 无有效授权条款（条款已失效）"
                % used_on.isoformat()
            )
        raise ValidationError("驳回：未登记该器具与方法的授权条款")

    spanning = [
        clause
        for clause in effective_clauses
        if _as_number(clause["data"]["lower_limit"], "lower_limit")
        <= value
        <= _as_number(clause["data"]["upper_limit"], "upper_limit")
    ]
    if not spanning:
        raise ValidationError(
            "驳回：条款越界，测得值 %s 不在任何有效量程的上下限内" % value
        )

    fitting = [
        clause
        for clause in spanning
        if uncertainty
        <= _as_number(clause["data"]["uncertainty_limit"], "uncertainty_limit")
    ]
    if not fitting:
        ceiling = min(
            _as_number(c["data"]["uncertainty_limit"], "uncertainty_limit")
            for c in spanning
        )
        raise ValidationError(
            "驳回：条款越界，扩展不确定度 %s 超过该量程不确定度上限 %s"
            % (uncertainty, ceiling)
        )

    # 多个量程重叠命中时，优先适用最窄量程，再按条款编号排序，保证结果确定。
    clause = min(fitting, key=lambda c: (_clause_span(c), c["data"]["clause_no"]))
    return {
        "released_by": actor.user_id,
        "clause_id": clause["id"],
        "clause_no": clause["data"]["clause_no"],
        "method_version": clause["data"].get("method_version"),
        "range_name": clause["data"].get("range_name"),
    }


CUSTOM_CREATE = {
    "calibration": _validate_calibration,
    "authorization": _validate_authorization,
}
CUSTOM_TRANSITIONS = {
    ("calibration", "perform"): _validate_perform,
    ("result", "release"): _validate_result_release,
}


class RuleEngine:
    ALIASES = {
        "instruments": "instrument",
        "calibrations": "calibration",
        "methods": "method",
        "results": "result",
        "authorizations": "authorization",
        "clauses": "authorization",
    }
    INITIAL_STATUS = {
        "instrument": "active",
        "calibration": "requested",
        "method": "draft",
        "authorization": "active",
        "result": "pending",
    }
    TRANSITIONS = {
        "instrument": {
            "send_calibration": (("active",), "calibrating"),
            "calibrate": (("calibrating",), "active"),
            "quarantine": (("active",), "quarantined"),
            "restore": (("quarantined",), "active"),
        },
        "calibration": {
            "perform": (("requested", "failed"), "passed"),
            "approve": (("passed",), "approved"),
            "reject": (("failed",), "rejected"),
        },
        "method": {
            "validate_method": (("draft",), "validated"),
            "revoke_method": (("validated",), "revoked"),
        },
        "authorization": {
            "withdraw": (("active",), "withdrawn"),
        },
        "result": {
            "release": (("pending",), "released"),
            "block": (("pending",), "blocked"),
            "reanalyze": (("blocked",), "pending"),
        },
    }
    CREATE_REQUIRED = {
        "instrument": ("name", "serial"),
        "calibration": ("instrument_id", "requested_at"),
        "method": ("name", "version"),
        "authorization": (
            "clause_no",
            "instrument_id",
            "method_id",
            "range_name",
            "lower_limit",
            "upper_limit",
            "uncertainty_limit",
            "expires_at",
        ),
        "result": ("sample_id", "measurement"),
    }
    ACTION_REQUIRED = {
        ("instrument", "calibrate"): ("due_at", "passed"),
        ("instrument", "quarantine"): ("reason",),
        ("calibration", "perform"): ("result", "performed_at", "uncertainty"),
        ("calibration", "approve"): ("authorized_by",),
        ("calibration", "reject"): ("reason",),
        ("method", "validate_method"): ("parameters", "instrument_ids"),
        ("method", "revoke_method"): ("reason",),
        ("authorization", "withdraw"): ("reason",),
        ("result", "release"): (
            "instrument_id",
            "method_id",
            "value",
            "expanded_uncertainty",
            "used_at",
        ),
        ("result", "block"): ("reason",),
        ("result", "reanalyze"): ("reason",),
    }
    CREATE_ROLES = {
        "instrument": ("admin", "technician"),
        "calibration": ("admin", "metrology"),
        "method": ("admin", "authorizer"),
        "authorization": ("admin", "authorizer"),
        "result": ("admin", "analyst"),
    }
    ROLE_ACTIONS = {
        "send_calibration": ("admin", "technician"),
        "calibrate": ("admin", "metrology"),
        "quarantine": ("admin", "metrology"),
        "restore": ("admin", "metrology"),
        "perform": ("admin", "metrology"),
        "approve": ("admin", "authorizer"),
        "reject": ("admin", "authorizer"),
        "validate_method": ("admin", "authorizer"),
        "revoke_method": ("admin", "authorizer"),
        "withdraw": ("admin", "authorizer"),
        "release": ("admin", "analyst"),
        "block": ("admin", "analyst"),
        "reanalyze": ("admin", "analyst"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
