"""HTTP/JSON 适配层。

只用标准库 ``http.server`` 实现，方便零依赖启动。
操作责任人通过请求头传递并随事件持久化：

* ``X-User-Id`` / ``X-User-Name``
* ``X-Role``（如 fleet_contact / reviewer / safety_officer / traffic_police）
* ``X-Jurisdiction``
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .clock import Clock
from .errors import DomainError, ValidationError
from .event_store import Actor, EventStore
from .serialization import parse_dt
from .service import PermitService


class ApiContainer:
    def __init__(self, store: EventStore, clock: Clock | None = None):
        self.store = store
        self.service = PermitService(store, clock)


Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class ApiHandler(BaseHTTPRequestHandler):
    container: ApiContainer  # 由工厂函数注入到类属性
    server_version = "PermitCoordination/1.0"

    routes: list[Route] = []

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，测试输出更干净
        return

    # ---- 框架 ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if match:
                try:
                    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    payload = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
                    result = handler(self, query=query, payload=payload, **match.groupdict())
                    if result is not None:
                        self._write_json(200, result)
                    return
                except DomainError as exc:
                    self._write_json(exc.status_code, exc.to_dict())
                    return
                except Exception as exc:  # 防御：未预期错误不返回堆栈给客户端
                    self._write_json(500, {"error": "internal_error", "message": str(exc)})
                    raise
        self._write_json(404, {"error": "not_found", "message": f"没有 {method} {parsed.path} 路由"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _write_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _actor(self, payload: dict[str, Any]) -> Actor:
        from urllib.parse import unquote

        user_id = self.headers.get("X-User-Id") or payload.pop("_actor_id", "")
        # HTTP 头只允许 latin-1，姓名按百分号编码传递（如 %E7%8E%8B%E9%98%9F）
        name = unquote(self.headers.get("X-User-Name", ""))
        role = self.headers.get("X-Role") or payload.pop("_actor_role", "")
        jurisdiction = self.headers.get("X-Jurisdiction", "")
        if not user_id or not role:
            raise ValidationError("缺少操作人头信息：X-User-Id 与 X-Role 必填")
        return Actor(user_id=user_id, role=role, jurisdiction=jurisdiction, name=name)

    @property
    def svc(self) -> PermitService:
        return self.container.service

    def _as_of(self, query: dict[str, str], key: str = "as_of") -> datetime | None:
        raw = query.get(key) or query.get("at")
        if not raw:
            return None
        return parse_dt(raw, key)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


def _register_routes(handler: type[ApiHandler]) -> None:
    P = re.compile

    def route(method: str, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        pattern = P(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", "^" + path + "$"))

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            handler.routes.append((method, pattern, fn))
            return fn

        return deco

    r = route

    @r("GET", "/healthz")
    def health(h: ApiHandler, **_: Any) -> dict[str, Any]:
        return {"status": "ok"}

    @r("POST", "/api/applications")
    def create_application(h: ApiHandler, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return h.svc.create_application(
            application_id=_need(payload, "application_id"),
            fleet_id=_need(payload, "fleet_id"),
            jurisdiction=_need(payload, "jurisdiction"),
            responsible=_need(payload, "responsible"),
            actor=h._actor(payload),
        )

    @r("GET", "/api/applications")
    def list_applications(h: ApiHandler, query: dict[str, str], **_: Any) -> dict[str, Any]:
        return h.svc.list_applications(state=query.get("state"))

    @r("GET", "/api/applications/{application_id}")
    def get_application(h: ApiHandler, query: dict[str, str], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.get_application_view(application_id, as_of=h._as_of(query))

    @r("GET", "/api/applications/{application_id}/effective")
    def effective(h: ApiHandler, query: dict[str, str], application_id: str, **_: Any) -> dict[str, Any]:
        at = h._as_of(query) or h.svc.clock.now()
        return h.svc.effective_view(application_id, at)

    @r("GET", "/api/applications/{application_id}/timeline")
    def timeline(h: ApiHandler, query: dict[str, str], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.timeline(application_id)

    @r("POST", "/api/applications/{application_id}/versions")
    def submit_version(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.submit_version(application_id, payload, h._actor(payload))

    @r("POST", "/api/applications/{application_id}/info-requests")
    def request_info(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.request_info(
            application_id,
            payload.get("questions", []),
            h._actor(payload),
        )

    @r("POST", "/api/applications/{application_id}/rule-checks")
    def rule_check(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.record_rule_check(application_id, h._actor(payload))

    @r("POST", "/api/applications/{application_id}/countersignatures")
    def countersign(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.countersign(
            application_id, str(payload.get("comment", "")), h._actor(payload)
        )

    @r("POST", "/api/applications/{application_id}/issue")
    def issue(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.issue_permit(application_id, h._actor(payload))

    @r("POST", "/api/applications/{application_id}/suspend")
    def suspend(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.suspend(application_id, _need(payload, "reason"), h._actor(payload))

    @r("POST", "/api/applications/{application_id}/resume")
    def resume(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.resume(application_id, str(payload.get("comment", "")), h._actor(payload))

    @r("POST", "/api/applications/{application_id}/revoke")
    def revoke(h: ApiHandler, payload: dict[str, Any], application_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.revoke(application_id, _need(payload, "reason"), h._actor(payload))

    @r("POST", "/api/closures")
    def register_closure(h: ApiHandler, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return h.svc.register_closure(payload, h._actor(payload))

    @r("GET", "/api/closures")
    def list_closures(h: ApiHandler, **_: Any) -> dict[str, Any]:
        return h.svc.list_closures()

    @r("GET", "/api/conflicts")
    def list_conflicts(h: ApiHandler, query: dict[str, str], **_: Any) -> dict[str, Any]:
        return h.svc.list_conflicts(status=query.get("status"))

    @r("GET", "/api/conflicts/{conflict_id}")
    def get_conflict(h: ApiHandler, query: dict[str, str], conflict_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.get_conflict_view(conflict_id)

    @r("POST", "/api/conflicts/{conflict_id}/proposal")
    def propose(h: ApiHandler, payload: dict[str, Any], conflict_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.propose_resolution(conflict_id, payload, h._actor(payload))

    @r("POST", "/api/conflicts/{conflict_id}/response")
    def respond(h: ApiHandler, payload: dict[str, Any], conflict_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.respond_resolution(
            conflict_id,
            bool(payload.get("accepted", False)),
            str(payload.get("comment", "")),
            h._actor(payload),
        )

    @r("POST", "/api/conflicts/{conflict_id}/countersign")
    def conflict_countersign(h: ApiHandler, payload: dict[str, Any], conflict_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.countersign_resolution(
            conflict_id, str(payload.get("comment", "")), h._actor(payload)
        )

    @r("POST", "/api/conflicts/{conflict_id}/close")
    def close_conflict(h: ApiHandler, payload: dict[str, Any], conflict_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.close_conflict(conflict_id, h._actor(payload))

    @r("POST", "/api/recognitions")
    def grant_recognition(h: ApiHandler, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return h.svc.grant_recognition(payload, h._actor(payload))

    @r("GET", "/api/recognitions")
    def list_recognitions(h: ApiHandler, query: dict[str, str], **_: Any) -> dict[str, Any]:
        return h.svc.list_recognitions(as_of=h._as_of(query))

    @r("GET", "/api/recognitions/{recognition_id}")
    def get_recognition(h: ApiHandler, query: dict[str, str], recognition_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.get_recognition_view(recognition_id, as_of=h._as_of(query))

    @r("POST", "/api/recognitions/{recognition_id}/amend")
    def amend_recognition(h: ApiHandler, payload: dict[str, Any], recognition_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.amend_recognition_scope(
            recognition_id,
            payload.get("scope", {}),
            str(payload.get("reason", "")),
            h._actor(payload),
        )

    @r("POST", "/api/recognitions/{recognition_id}/withdraw")
    def withdraw_recognition(h: ApiHandler, payload: dict[str, Any], recognition_id: str, **_: Any) -> dict[str, Any]:
        return h.svc.withdraw_recognition(
            recognition_id, _need(payload, "reason"), h._actor(payload)
        )

    @r("GET", "/api/audit/events")
    def audit_events(h: ApiHandler, query: dict[str, str], **_: Any) -> dict[str, Any]:
        as_of = h._as_of(query)
        events = h.container.store.all_events(as_of)
        return {
            "events": [
                {
                    "seq": e.seq,
                    "event_id": e.event_id,
                    "stream_id": e.stream_id,
                    "stream_version": e.version,
                    "type": e.event_type,
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "data": e.data,
                }
                for e in events
            ],
            "count": len(events),
        }


def _need(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"缺少必填字段 {key}")
    return value


def build_server(
    host: str,
    port: int,
    container: ApiContainer,
) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"routes": [], "container": container})
    _register_routes(handler)
    server = ThreadingHTTPServer((host, port), handler)
    return server
