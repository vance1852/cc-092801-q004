"""版本化海外授权与收益台账的 HTTP JSON 接口（仅依赖标准库）。"""
from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import LicensingError, ValidationFailed
from .service_ledger import LicensingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class LedgerApplication:
    def __init__(self, service: LicensingService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    # pylint: disable=too-complex
    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None,
               body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "licensing-ledger"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            s = self.service
            # 用户引导是部署入口，不要求已存在的 X-Actor-Id（服务层不鉴权）。
            if method == "POST" and path == "/users":
                return Response(201, s.create_user("", payload["user_id"], payload["display_name"], payload["role"]))
            actor = self._actor(normalized)

            if method == "POST" and path == "/candidates":
                return Response(201, s.register_candidate(actor, payload["candidate_id"], payload["name"], payload.get("internal_code")))
            if method == "POST" and path == "/parties":
                return Response(201, s.register_party(actor, payload["party_id"], payload["name"], payload["kind"]))

            if method == "POST" and path == "/agreements":
                return Response(201, s.create_agreement(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "agreements":
                return Response(200, s.agreement(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "agreements" and parts[2] == "versions":
                return Response(200, {"versions": s.list_versions(actor, parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "agreements" and parts[2] == "versions":
                return Response(201, s.draft_version(actor, parts[1], payload))
            if method == "POST" and len(parts) == 4 and parts[0] == "agreements" and parts[3] == "terminate":
                return Response(200, s.terminate_agreement(actor, parts[1], payload.get("note", "")))
            if method == "GET" and len(parts) == 3 and parts[0] == "agreements" and parts[2] == "obligations":
                return Response(200, s.list_obligations(actor, parts[1]))
            if (method == "POST" and len(parts) == 5 and parts[0] == "agreements"
                    and parts[2] == "obligations" and parts[4] == "satisfy"):
                return Response(200, s.satisfy_obligation(actor, parts[1], parts[3], payload.get("note", "")))
            if method == "GET" and path == "/obligations/unmet":
                return Response(200, s.unmet_obligations(actor, query.get("candidate_id", [None])[0]))

            if method == "POST" and len(parts) == 3 and parts[0] == "versions" and parts[2] == "review":
                return Response(200, s.run_review(actor, parts[1], payload.get("waived_obligation_ids")))
            if method == "POST" and len(parts) == 3 and parts[0] == "versions" and parts[2] == "sign":
                return Response(201, s.sign_version(actor, parts[1], payload.get("waived_obligation_ids")))
            if method == "POST" and len(parts) == 3 and parts[0] == "versions" and parts[2] == "discard":
                return Response(200, s.discard_version(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "versions":
                return Response(200, s.version(actor, parts[1]))

            if method == "POST" and path == "/assumptions":
                return Response(201, s.create_assumptions(actor, payload["assumptions_id"], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "assumptions" and parts[2] == "forecast":
                return Response(200, s.forecast(actor, parts[1], payload["as_of_date"]))

            if method == "POST" and path == "/events":
                return Response(201, s.record_event(actor, payload, payload.get("idempotency_key")))
            if method == "GET" and len(parts) == 2 and parts[0] == "events":
                return Response(200, s.event(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "events" and parts[2] == "disputes":
                return Response(201, s.open_dispute(actor, parts[1], payload["reason"], payload["recipient_party_ids"]))

            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, s.resolve_dispute(actor, parts[1], payload.get("resolution_note", "")))

            if method == "GET" and len(parts) == 2 and parts[0] == "distributions" and parts[1] != "":
                return Response(200, s.distribution(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "confirm":
                return Response(200, s.confirm_distribution(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "settle":
                return Response(200, s.settle_distribution(actor, parts[1], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "reverse":
                return Response(200, s.reverse_distribution(actor, parts[1], payload.get("note", "")))
            if method == "GET" and len(parts) == 3 and parts[0] == "distributions" and parts[2] == "basis":
                return Response(200, s.distribution_basis(actor, parts[1]))

            if method == "GET" and len(parts) == 3 and parts[0] == "agreements" and parts[2] == "chain":
                return Response(200, s.authorization_chain(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "regions" and parts[2] == "trace":
                return Response(200, s.trace_region(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "candidates" and parts[2] == "trace":
                return Response(200, s.trace_candidate(actor, parts[1]))
            if method == "GET" and path == "/ledger":
                return Response(200, s.ledger_report(
                    actor, period=query.get("period", [None])[0],
                    recipient_party_id=query.get("recipient_party_id", [None])[0]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, s.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LicensingError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: LedgerApplication):
    # 所有线程共享一个 SQLite 连接；用锁串行化分发，避免同连接上事务嵌套与语句交错。
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LicensingLedger/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with dispatch_lock:
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
    parser = argparse.ArgumentParser(description="启动版本化海外授权与收益台账服务")
    parser.add_argument("--database", type=Path, default=Path("licensing_ledger.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    # 线程化 HTTP 服务共享单连接；写事务统一 BEGIN IMMEDIATE 串行化，WAL 允许并发读。
    connection = connect(str(args.database), check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(LedgerApplication(LicensingService(connection))))
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
