"""ThirdEye module adapter for LiteView.

This module intentionally stays lightweight: it exposes a minimal API that can be
plugged into the host server without depending on the rest of the monitor logic.
The LiteView app already handles frame capture and input injection; this adapter
provides the optional module status and exposure points that a ThirdEye-style
integration expects.
"""

from __future__ import annotations

from typing import Any, Callable

from aiohttp import web


class ThirdEyeModule:
    """Optional ThirdEye-style integration layer for the host app."""

    def __init__(self, name: str = "ThirdEye"):
        self.name = name
        self.enabled = True
        self.metadata: dict[str, Any] = {
            "module": "thirdeye",
            "module_type": "module_2",
            "status": "enabled",
            "interface": "liteview",
        }

    def register(self, app: web.Application, frame_provider: Callable | None = None, **kwargs: Any) -> "ThirdEyeModule":
        app["third_eye"] = self
        app.setdefault("modules", [])
        if self.name not in app["modules"]:
            app["modules"].append(self.name)

        app.router.add_get("/module/third-eye", self.status)
        app.router.add_get("/module/third-eye/health", self.health)
        app.router.add_get("/module/third-eye/info", self.info)
        app["third_eye_provider"] = frame_provider
        app["third_eye_metadata"] = {**self.metadata, **kwargs}
        return self

    async def status(self, request: web.Request) -> web.Response:
        payload = {
            "module": self.name,
            "status": "enabled",
            "type": "module_2",
            "host": request.host,
            "capture_ready": bool(request.app.get("capture_pool")),
        }
        return web.json_response(payload)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"module": self.name, "ok": True, "state": "healthy"})

    async def info(self, request: web.Request) -> web.Response:
        payload = {
            **self.metadata,
            "path": "/module/third-eye",
            "frame_provider": "available" if request.app.get("third_eye_provider") else "missing",
        }
        return web.json_response(payload)


__all__ = ["ThirdEyeModule"]
