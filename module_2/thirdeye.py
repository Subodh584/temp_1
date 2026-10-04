"""ThirdEye module adapter for LiteView.

This is a lightweight integration layer that hooks into the existing screen capture
stream and exposes a small API with actual behavior: it records the latest frame,
adds an overlay watermark, and publishes metadata so the LiteView app can expose
ThirdEye state to the browser.
"""

from __future__ import annotations

import hashlib
import io
import time
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont
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
        self.last_frame = None
        self.last_frame_bytes = b""
        self.last_timestamp = 0.0
        self.frame_count = 0
        self.last_digest = None

    def register(self, app: web.Application, frame_provider: Callable | None = None, **kwargs: Any) -> "ThirdEyeModule":
        app["third_eye"] = self
        app["third_eye_active"] = True
        app.setdefault("modules", [])
        if self.name not in app["modules"]:
            app["modules"].append(self.name)

        app.router.add_get("/module/third-eye", self.status)
        app.router.add_get("/module/third-eye/health", self.health)
        app.router.add_get("/module/third-eye/info", self.info)
        app.router.add_get("/module/third-eye/latest", self.latest)
        app.router.add_get("/module/third-eye/frame", self.frame)

        app["third_eye_provider"] = frame_provider
        app["third_eye_metadata"] = {**self.metadata, **kwargs}
        return self

    def observe(self, jpeg_bytes: bytes) -> dict[str, Any]:
        if not jpeg_bytes:
            return {"module": self.name, "status": "idle"}
        digest = hashlib.blake2b(jpeg_bytes, digest_size=12).hexdigest()
        self.frame_count += 1
        self.last_timestamp = time.time()
        self.last_digest = digest
        self.last_frame_bytes = jpeg_bytes
        self.last_frame = self._annotate(jpeg_bytes, label=f"{self.name} live")
        return {
            "module": self.name,
            "status": "tracking",
            "frame_count": self.frame_count,
            "last_seen": self.last_timestamp,
            "digest": digest,
            "size": len(jpeg_bytes),
        }

    def _annotate(self, jpeg_bytes: bytes, label: str) -> bytes:
        try:
            image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGBA")
            width, height = image.size
            overlay = Image.new("RGBA", (width, max(height, 80)), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", max(18, width // 80))
            except Exception:
                font = ImageFont.load_default()
            draw.text((18, 12), label, font=font, fill=(255, 255, 255, 180))
            image = Image.alpha_composite(image, overlay)
            bio = io.BytesIO()
            image.convert("RGB").save(bio, format="JPEG", quality=85)
            return bio.getvalue()
        except Exception:
            return jpeg_bytes

    async def status(self, request: web.Request) -> web.Response:
        payload = {
            "module": self.name,
            "status": "enabled",
            "type": "module_2",
            "host": request.host,
            "capture_ready": bool(request.app.get("capture_pool")),
            "frame_count": self.frame_count,
            "active": self.enabled,
        }
        return web.json_response(payload)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"module": self.name, "ok": True, "state": "healthy", "frames_seen": self.frame_count})

    async def info(self, request: web.Request) -> web.Response:
        payload = {
            **self.metadata,
            "path": "/module/third-eye",
            "frame_provider": "available" if request.app.get("third_eye_provider") else "missing",
            "last_digest": self.last_digest,
            "last_seen": self.last_timestamp,
        }
        return web.json_response(payload)

    async def latest(self, request: web.Request) -> web.Response:
        payload = {
            "module": self.name,
            "active": self.enabled,
            "status": "tracking" if self.last_frame_bytes else "idle",
            "last_seen": self.last_timestamp,
            "frame_count": self.frame_count,
            "digest": self.last_digest,
            "size": len(self.last_frame_bytes),
        }
        return web.json_response(payload)

    async def frame(self, request: web.Request) -> web.Response:
        if not self.last_frame:
            return web.Response(status=204)
        return web.Response(body=self.last_frame, content_type="image/jpeg")


__all__ = ["ThirdEyeModule"]
