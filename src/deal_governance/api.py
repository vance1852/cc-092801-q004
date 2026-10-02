"""版本化授权与收益治理的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import Conflict, DealGovernanceError, SigningBlocked
from .service import DealGovernanceService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    def __init__(self, service: DealGovernanceService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            from .errors import ValidationFailed
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            from .errors import ValidationFailed
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            from .errors import ValidationFailed
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str,
               headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "deal-governance"})
            actor = self._actor(normalized)
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            s = self.service

            if method == "POST" and path == "/bootstrap-user":
                return Response(201, s.create_user(None, payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/candidates":
                return Response(201, s.register_candidate(actor, payload))
            if method == "POST" and path == "/parties":
                return Response(201, s.register_party(actor, payload))
            if method == "POST" and path == "/agreements":
                return Response(201, s.create_agreement(actor, payload["agreement_id"], payload["candidate_id"], payload["title"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "agreements" and parts[2] == "revisions":
                return Response(201, s.draft_revision(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "pre-sign":
                return Response(200, {"revision_id": parts[1], "findings": s.pre_sign_findings(actor, parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "sign":
                return Response(200, s.sign_revision(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "terminate":
                return Response(200, s.terminate_revision(actor, parts[1], payload.get("reason", "")))
            if method == "GET" and len(parts) == 2 and parts[0] == "revisions":
                return Response(200, s.get_revision(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "revisions" and parts[2] == "chain":
                return Response(200, s.rights_chain(actor, parts[1]))
            if method == "POST" and len(parts) == 4 and parts[0] == "revisions" and parts[2] == "obligations" and parts[3] == "fulfill":
                return Response(200, s.fulfill_obligation(actor, parts[1], payload["obligation_id"], payload["note"], payload.get("evidence_ref")))
            if method == "POST" and path == "/projections":
                return Response(201, s.create_projection(actor, payload["revision_id"], payload["assumptions"], commit=bool(payload.get("commit"))))
            if method == "GET" and len(parts) == 2 and parts[0] == "projections":
                return Response(200, s.get_projection(actor, parts[1]))
            if method == "POST" and path == "/payment-events":
                return Response(201, s.record_payment_event(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "payment-events":
                return Response(200, s.get_event(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "allocations" and parts[2] == "settle":
                return Response(200, s.settle_allocation(actor, parts[1], payload["settlement_ref"]))
            if method == "POST" and path == "/disputes":
                return Response(201, s.open_dispute(actor, payload["event_id"], payload["allocation_ids"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, s.resolve_dispute(actor, parts[1], payload["outcome"], payload.get("adjustments"), payload.get("note", "")))
            if method == "GET" and len(parts) == 2 and parts[0] == "disputes":
                return Response(200, s.get_dispute(actor, parts[1]))
            if method == "GET" and path == "/trace/rights":
                return Response(200, {"revisions": s.territory_trace(actor, query.get("candidate_id", [None])[0], query.get("territory", [None])[0])})
            if method == "GET" and path == "/trace/obligations":
                return Response(200, s.open_obligations(actor, candidate_id=query.get("candidate_id", [None])[0], territory=query.get("territory", [None])[0]))
            if method == "GET" and path == "/trace/allocations":
                return Response(200, {"allocations": s.allocation_basis(actor, recipient_party_id=query.get("recipient_party_id", [None])[0], candidate_id=query.get("candidate_id", [None])[0])})
            if method == "GET" and path == "/audit/chain":
                return Response(200, {"events": s.audit_chain(actor)})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except SigningBlocked as exc:
            return Response(422, {"error": {"code": exc.code, "message": str(exc), "findings": exc.findings}})
        except DealGovernanceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DealGovernance/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动版本化海外授权与收益回流治理服务")
    parser.add_argument("--database", type=Path, default=Path("deal_governance.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    service = DealGovernanceService(database=str(args.database))
    for user in (("bd", "商务拓展", "bd"), ("manager", "授权管理", "manager"),
                 ("finance", "财务确认", "finance"), ("auditor", "审计追溯", "auditor")):
        try:
            service.create_user(None, *user)
        except Conflict:
            pass
    connection = service.db  # 仅用于退出时关闭当前线程连接
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(service)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
