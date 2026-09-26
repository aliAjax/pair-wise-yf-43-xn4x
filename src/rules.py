from datetime import date, datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ReleaseRejected,
    ValidationError,
)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_authorization(actor, data, lookup):
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        raise ValidationError("authorization requires an existing method")
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("authorization requires an existing instrument")

    lower = _number(data.get("lower_limit"), "lower_limit")
    upper = _number(data.get("upper_limit"), "upper_limit")
    if lower >= upper:
        raise ValidationError("lower_limit must be smaller than upper_limit")
    _number(data.get("max_uncertainty"), "max_uncertainty")
    if data.get("max_uncertainty") < 0:
        raise ValidationError("max_uncertainty must be non-negative")
    _parse_date(data.get("valid_until"), "valid_until")

    # A clause number is unique within one method version.
    clause_no = data.get("clause_no")
    for other in lookup("authorization", "method_id", data.get("method_id")) or []:
        if other["data"].get("clause_no") == clause_no and other["status"] == "active":
            raise ValidationError(
                "active clause %s already exists for this method" % clause_no
            )


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _validate_result_release(actor, entity, data, lookup):
    # 1. Instrument must exist and be in service.
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ReleaseRejected("instrument_missing", "instrument does not exist")
    if instrument["status"] != "active":
        raise ReleaseRejected(
            "instrument_not_active",
            "instrument is %s, only active instruments can be used" % instrument["status"],
        )

    # 2. Calibration must have passed and still be current on the use date.
    calibrations = lookup("calibration", "instrument_id", instrument["id"]) or []
    latest = max(
        calibrations,
        key=lambda item: str(item["data"].get("performed_at") or item["created_at"]),
        default=None,
    )
    if not latest:
        raise ReleaseRejected(
            "calibration_missing", "instrument has no calibration record"
        )
    if latest["status"] in ("failed", "rejected") or latest["data"].get("result") == "failed":
        raise ReleaseRejected(
            "calibration_failed", "latest calibration of the instrument failed"
        )
    if latest["status"] not in ("passed", "approved"):
        raise ReleaseRejected(
            "calibration_missing", "instrument has no passed calibration record"
        )
    due_at = latest["data"].get("due_at")
    used_at = data.get("used_at")
    if not due_at or not calibration_current(due_at, used_at):
        raise ReleaseRejected(
            "calibration_expired",
            "instrument calibration is not current on %s" % used_at,
        )

    # 3. Method must still be validated (a revoked method rejects every release).
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        raise ReleaseRejected("method_missing", "method does not exist")
    if method["status"] == "revoked":
        raise ReleaseRejected(
            "method_revoked", "method %s has been revoked" % method["data"].get("version")
        )
    if method["status"] != "validated":
        raise ReleaseRejected(
            "method_not_validated", "method is %s, not validated" % method["status"]
        )

    # 4. The reading must hit one active, in-date authorization clause for
    #    this instrument and method; its uncertainty ceiling must also hold.
    value = _number(data.get("value"), "value")
    uncertainty = _number(data.get("expanded_uncertainty"), "expanded_uncertainty")
    if uncertainty < 0:
        raise ValidationError("expanded_uncertainty must be non-negative")
    use_date = _parse_date(used_at, "used_at")

    clauses = [
        item
        for item in lookup("authorization", "method_id", method["id"]) or []
        if item["status"] == "active"
        and item["data"].get("instrument_id") == instrument["id"]
    ]
    if not clauses:
        raise ReleaseRejected(
            "clause_missing",
            "no authorization clause for this instrument and method",
        )

    in_range = [
        item
        for item in clauses
        if item["data"].get("lower_limit") <= value <= item["data"].get("upper_limit")
    ]
    if not in_range:
        raise ReleaseRejected(
            "value_out_of_range",
            "measured value %s is outside every authorized range" % value,
        )

    uncertainty_ok = [
        item
        for item in in_range
        if uncertainty <= item["data"].get("max_uncertainty")
    ]
    if not uncertainty_ok:
        raise ReleaseRejected(
            "uncertainty_exceeded",
            "expanded uncertainty %s exceeds the clause ceiling" % uncertainty,
        )

    in_date = [
        item
        for item in uncertainty_ok
        if _parse_date(item["data"].get("valid_until"), "valid_until") >= use_date
    ]
    if not in_date:
        raise ReleaseRejected(
            "clause_expired",
            "every matching clause had expired on %s" % used_at,
        )

    # Narrowest matching range wins when ranges overlap.
    matched = min(
        in_date,
        key=lambda item: item["data"].get("upper_limit")
        - item["data"].get("lower_limit"),
    )
    clause = matched["data"]
    return {
        "released_by": actor.user_id,
        "authorization_id": matched["id"],
        "clause_no": clause.get("clause_no"),
        "clause_version": clause.get("clause_version"),
        "method_version": method["data"].get("version"),
    }


CUSTOM_CREATE = {
    'calibration': _validate_calibration,
    'authorization': _validate_authorization,
}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('result', 'release'): _validate_result_release,
}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'authorizations': 'authorization'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'authorization': 'active'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'authorization': {'revoke': (('active',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'authorization': ('method_id', 'instrument_id', 'clause_no', 'clause_version', 'range_name', 'lower_limit', 'upper_limit', 'max_uncertainty', 'valid_until'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters',), ('method', 'revoke_method'): ('reason',), ('authorization', 'revoke'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'expanded_uncertainty', 'used_at'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'authorization': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'revoke': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

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


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s must be a number" % field)
    return value


def _parse_date(value, field):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise ValidationError("%s must be an ISO date (YYYY-MM-DD)" % field)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
