"""HTTP/JSON 接口（仅依赖标准库 http.server）。

所有写操作通过 X-Actor 头记录责任人；时间可通过 X-Current-Time 头注入（演练/审计回放），
生产部署不设置该头时使用系统时钟。
"""

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from permit_coordination.clock import SystemClock
from permit_coordination.errors import DomainError
from permit_coordination.service import PermitService
from permit_coordination.store import EventStore, InMemoryEventStore, JsonFileEventStore

def parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("时间必须携带时区信息，例如 2026-10-21T08:00:00+00:00")
    return at


class PermitHTTPHandler(BaseHTTPRequestHandler):
    server_version = "PermitCoordination/1.0"

    # 由 server 注入
    service: PermitService

    def log_message(self, fmt, *args):  # 安静：测试输出不污染
        return

    # ------------------------------------------------------------ 基础件

    def _send_json(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _HttpError(400, "invalid_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(body, dict):
            raise _HttpError(400, "invalid_body", "请求体必须为 JSON 对象")
        return body

    def _actor(self, body: dict) -> str:
        actor = self.headers.get("X-Actor") or body.pop("_actor", None)
        if not actor:
            raise _HttpError(400, "actor_required", "写操作必须通过 X-Actor 头标识责任人")
        return actor

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _query_at(self):
        raw = self._query().get("at", [None])[0]
        try:
            return parse_at(raw)
        except ValueError as exc:
            raise _HttpError(400, "bad_time", str(exc))

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            handler = self._match(method, path)
            if handler is None:
                self._send_json(404, {"error": "not_found", "message": f"无此路由: {method} {path}"})
                return
            handler()
        except _HttpError as exc:
            self._send_json(exc.status, {"error": exc.code, "message": exc.message})
        except DomainError as exc:
            self._send_json(exc.http_status, exc.to_body())
        except ValueError as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})

    def _match(self, method: str, path: str):
        routes = GET_ROUTES if method == "GET" else POST_ROUTES
        for pattern, handler_name in routes:
            match = pattern.fullmatch(path)
            if match:
                return lambda: getattr(self, handler_name)(**match.groupdict())
        return None

    # ------------------------------------------------------------ GET 接口

    def handle_health(self):
        self._send_json(200, {"status": "ok"})

    def handle_list_permits(self):
        at = self._query_at()
        self._send_json(200, {"permits": self.service.effective_permits(at)})

    def handle_get_permit(self, permit_id):
        self._send_json(200, self.service.permit_detail(permit_id))

    def handle_get_review(self, review_id):
        self._send_json(200, self.service.review_detail(review_id))

    def handle_get_application(self, application_id):
        self._send_json(200, self.service.history(application_id))

    def handle_list_conflicts(self):
        state = self._query().get("state", [None])[0]
        self._send_json(200, {"conflicts": self.service.list_conflicts(state=state)})

    def handle_snapshot(self):
        at = self._query_at()
        if at is None:
            raise _HttpError(400, "at_required", "snapshot 必须提供 at 查询参数")
        self._send_json(200, self.service.snapshot_at(at))

    def handle_journal(self):
        self._send_json(200, {"events": self.service.event_journal()})

    # ------------------------------------------------------------ POST 接口

    def handle_submit(self):
        body = self._read_json()
        actor = self._actor(body)
        result = self.service.submit_application(body, actor=actor)
        self._send_json(201, result)

    def handle_open_review(self):
        body = self._read_json()
        actor = self._actor(body)
        try:
            application_id = body["application_id"]
            version = int(body["version"])
            jurisdiction = body["jurisdiction"]
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpError(400, "bad_request", f"需要 application_id/version/jurisdiction: {exc}")
        self._send_json(201, self.service.open_review(
            application_id, version, jurisdiction, actor=actor))

    def handle_supplement_request(self, review_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(201, self.service.request_supplement(
            review_id, body.get("findings", []), body.get("requested_items", []), actor=actor))

    def handle_supplement_submit(self, review_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(201, self.service.submit_supplement(
            review_id, body.get("documents", []),
            body.get("resolved_finding_codes", []), actor=actor))

    def handle_approve(self, review_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.approve_review(
            review_id, body.get("conditions") or [], actor=actor))

    def handle_reject(self, review_id):
        body = self._read_json()
        actor = self._actor(body)
        reason = body.get("reason", "")
        self._send_json(200, self.service.reject_review(review_id, reason, actor=actor))

    def handle_issue(self):
        body = self._read_json()
        actor = self._actor(body)
        try:
            result = self.service.issue_permit(
                body["application_id"], int(body["version"]), actor=actor,
                permit_id=body.get("permit_id"))
        except KeyError as exc:
            raise _HttpError(400, "bad_request", f"缺少字段: {exc.args[0]}")
        self._send_json(201, result)

    def handle_suspend(self, permit_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.suspend_permit(
            permit_id, body.get("reason", ""), actor=actor))

    def handle_reinstate(self, permit_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.reinstate_permit(permit_id, actor=actor))

    def handle_revoke(self, permit_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.revoke_permit(
            permit_id, body.get("reason", ""), actor=actor))

    def handle_record_recognition(self):
        body = self._read_json()
        actor = self._actor(body)
        try:
            result = self.service.record_recognition(
                body["permit_id"], body["recognizing_jurisdiction"],
                body.get("scope", {}), body.get("basis", ""), actor=actor)
        except KeyError as exc:
            raise _HttpError(400, "bad_request", f"缺少字段: {exc.args[0]}")
        self._send_json(201, result)

    def handle_revoke_recognition(self, recognition_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.revoke_recognition(
            recognition_id, body.get("reason", ""), actor=actor))

    def handle_road_closure(self):
        body = self._read_json()
        actor = self._actor(body)
        try:
            result = self.service.register_road_closure(
                body["segment_codes"], body["starts_at"], body["ends_at"],
                body.get("reason", ""), actor=actor)
        except KeyError as exc:
            raise _HttpError(400, "bad_request", f"缺少字段: {exc.args[0]}")
        self._send_json(201, result)

    def handle_resolve_conflict(self, conflict_id):
        body = self._read_json()
        actor = self._actor(body)
        self._send_json(200, self.service.resolve_conflict(
            conflict_id, body.get("resolution", ""), actor=actor,
            new_window=body.get("new_window"), note=body.get("note", "")))


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _build_routes():
    token = r"(?P<[a-z_]+>[A-Za-z0-9][A-Za-z0-9._-]*)"

    def pat(pattern: str):
        return re.compile("^" + pattern.replace("{id}", r"(?P<permit_id>[A-Za-z0-9._-]+)")
                          .replace("{rid}", r"(?P<review_id>[A-Za-z0-9._-]+)")
                          .replace("{cid}", r"(?P<conflict_id>[A-Za-z0-9._-]+)")
                          .replace("{aid}", r"(?P<application_id>[A-Za-z0-9._-]+)")
                          .replace("{gid}", r"(?P<recognition_id>[A-Za-z0-9._-]+)") + "$")

    get_routes = [
        (re.compile(r"^/healthz$"), "handle_health"),
        (re.compile(r"^/permits$"), "handle_list_permits"),
        (pat(r"/permits/{id}"), "handle_get_permit"),
        (pat(r"/reviews/{rid}"), "handle_get_review"),
        (pat(r"/applications/{aid}/history"), "handle_get_application"),
        (re.compile(r"^/conflicts$"), "handle_list_conflicts"),
        (re.compile(r"^/snapshot$"), "handle_snapshot"),
        (re.compile(r"^/journal$"), "handle_journal"),
    ]
    post_routes = [
        (re.compile(r"^/applications/submissions$"), "handle_submit"),
        (re.compile(r"^/reviews$"), "handle_open_review"),
        (pat(r"/reviews/{rid}/supplement-request"), "handle_supplement_request"),
        (pat(r"/reviews/{rid}/supplements"), "handle_supplement_submit"),
        (pat(r"/reviews/{rid}/approve"), "handle_approve"),
        (pat(r"/reviews/{rid}/reject"), "handle_reject"),
        (re.compile(r"^/permits/issue$"), "handle_issue"),
        (pat(r"/permits/{id}/suspend"), "handle_suspend"),
        (pat(r"/permits/{id}/reinstate"), "handle_reinstate"),
        (pat(r"/permits/{id}/revoke"), "handle_revoke"),
        (re.compile(r"^/recognitions$"), "handle_record_recognition"),
        (pat(r"/recognitions/{gid}/revoke"), "handle_revoke_recognition"),
        (re.compile(r"^/road-closures$"), "handle_road_closure"),
        (pat(r"/conflicts/{cid}/resolve"), "handle_resolve_conflict"),
    ]
    return get_routes, post_routes


GET_ROUTES, POST_ROUTES = _build_routes()


def build_server(host: str = "127.0.0.1", port: int = 8080,
                 store: EventStore | None = None,
                 clock=None,
                 jsonl_path: str | None = None) -> ThreadingHTTPServer:
    store = store or (JsonFileEventStore(jsonl_path) if jsonl_path else InMemoryEventStore())
    service = PermitService(store, clock=clock or SystemClock())

    class _Handler(PermitHTTPHandler):
        pass

    _Handler.service = service
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.service = service
    httpd.store = store
    return httpd


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="道路测试许可协同 HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--event-log", default=None, help="事件 JSONL 持久化路径")
    args = parser.parse_args()

    httpd = build_server(args.host, args.port, jsonl_path=args.event_log)
    print(f"道路测试许可协同服务已启动: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
