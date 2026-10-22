"""为本机 HTTP 入口统一检查 Host、浏览器来源和访问令牌。"""

import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LocalAccessMiddleware:
    """在读取请求正文前拒绝非法来源或令牌；健康检查仅公开无敏感信息的状态。"""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app: ASGIApp = app
        self._credential = ("Bearer " + token).encode("ascii")
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{host}" for host in self._hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """为 HTTP 请求检查来源和令牌，其他 ASGI 生命周期消息直接传递。"""
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            status = None
            if headers.get("host") not in self._hosts:
                status = 421
            elif headers.get("origin") is not None and headers["origin"] not in self._origins:
                status = 403
            elif scope["path"] != "/health":
                credential = headers.get("authorization", "").encode("latin-1")
                if not secrets.compare_digest(credential, self._credential):
                    status = 401
            if status is not None:
                await JSONResponse({"error": "request denied"}, status_code=status)(scope, receive, send)
                return
        await self.app(scope, receive, send)
