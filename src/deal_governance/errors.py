"""授权治理服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations


class DealGovernanceError(RuntimeError):
    code = "deal_error"
    status = 400


class NotFound(DealGovernanceError):
    code = "not_found"
    status = 404


class Conflict(DealGovernanceError):
    code = "conflict"
    status = 409


class Forbidden(DealGovernanceError):
    code = "forbidden"
    status = 403


class InvalidState(DealGovernanceError):
    code = "invalid_state"
    status = 409


class SigningBlocked(DealGovernanceError):
    code = "signing_blocked"
    status = 422

    def __init__(self, findings: list[dict]) -> None:
        super().__init__("签署前检查存在阻断项")
        self.findings = findings


class ValidationFailed(DealGovernanceError):
    code = "validation_failed"
    status = 422
