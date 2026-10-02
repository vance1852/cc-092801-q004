"""版本化授权与收益台账向 API 和 CLI 暴露的稳定错误。"""
from __future__ import annotations


class LicensingError(RuntimeError):
    code = "licensing_error"
    status = 400


class NotFound(LicensingError):
    code = "not_found"
    status = 404


class Conflict(LicensingError):
    code = "conflict"
    status = 409


class Forbidden(LicensingError):
    code = "forbidden"
    status = 403


class InvalidState(LicensingError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LicensingError):
    code = "validation_failed"
    status = 422


class ReviewBlocked(LicensingError):
    """签署前评审仍存在阻断项。"""

    code = "review_blocked"
    status = 422
