# SPDX-License-Identifier: Apache-2.0
"""Entry point for the streaming USD viewer server.

OVRTX_SKIP_USD_CHECK=1 MUST be set before any module that can import ovrtx or pxr.
We set it here, at the very top, before importing anything from this package.
"""
from __future__ import annotations

import os

# ── Must be first — before any ovrtx / pxr import ──────────────────────────
os.environ["OVRTX_SKIP_USD_CHECK"] = "1"

import json as _json
import logging
import mimetypes
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Static file root — the Vite build output ────────────────────────────────
_HERE      = Path(__file__).parent          # server/
_DIST_ROOT = _HERE.parent / "frontend-react" / "dist"

from .config import Events, ServerConfig
from .ov_web_viewer_server import OVWebViewerServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("viewer.main")


# ── HTTP API server ──────────────────────────────────────────────────────────
class _ApiHandler(BaseHTTPRequestHandler):
    """HTTP handler for the viewer REST API + legacy /healthz + /camera.

    Routes
    ──────
    GET  /healthz                       503 until first frame, then 200
    GET  /api/status                    current server state JSON
    GET  /api/scenes                    list of available USD scenes
    GET  /api/hierarchy?path=<path>     USD prim children at <path>
    GET  /api/prim?path=<path>          prim properties (type, xform, attrs)
    GET  /api/prim/variants?path=<path> variant sets for a prim
    GET  /api/prim/bbox?path=<path>     world-space bounding box
    GET  /api/search?q=<query>          search prims by name/type
    GET  /api/timeline                  animation timeline state
    GET  /api/render/mode               current render mode
    GET  /api/snapshot                  current frame as PNG download
    GET  /api/bookmarks                 list of saved camera bookmarks
    POST /camera                        legacy camera command (zoom)
    POST /api/scene                     load a USD scene  { "path": "..." }
    POST /api/pick                      blocking pick     { "x": N, "y": N }
    POST /api/prim/xform                set prim transform { "path", "translate"?, "rotate"?, "scale"? }
    POST /api/prim/visibility           set visibility    { "path", "visible": bool }
    POST /api/prim/variant              select variant    { "path", "variant_set", "variant" }
    POST /api/measure                   measure distance  { "path_a", "path_b" }
    POST /api/timeline                  control playback  { "time_code"?, "playing"?, "speed"? }
    POST /api/render/mode               set render mode   { "mode": "rtx"|"unlit"|"wireframe" }
    POST /api/create_prim               create prim       { "type": "Sphere"|"Cube"|..., "name": "..." }
    POST /api/deactivate_prim           deactivate prim   { "path": "/World/..." }
    POST /api/undo                      undo last edit    {}
    POST /api/redo                      redo last undo    {}
    POST /api/save                      export stage      { "output_path": "..." }
    GET  /api/telemetry/prims           discover Xformable prims for binding
    POST /api/telemetry/generate        generate + play   { "bindings": [...], "duration"?, "fps"? }
    POST /api/telemetry/stop            stop + reload original scene
    POST /api/bookmarks                 save current view { "name": "..." }
    POST /api/bookmarks/recall/<name>   restore bookmark
    DELETE /api/bookmarks/<name>        delete bookmark

    CORS headers on every response so the browser can reach the API from
    file:// or any other origin (the page is not always same-origin as :8081).
    """

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def _ov(self) -> OVWebViewerServer:
        return self.server.ov_server  # type: ignore[attr-defined]

    @property
    def _cmd_q(self):
        return self.server.cmd_queue  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data: dict | list, status: int = 200) -> None:
        body = _json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        return _json.loads(body) if body else {}

    def _not_found(self) -> None:
        self._send_json({"error": "not found"}, 404)

    def _serve_static(self, rel_path: str) -> None:
        """Serve a file from the Vite dist/ folder.

        Any path that isn't an API route is tried against dist/.
        If not found, fall back to dist/index.html (SPA client-side routing).
        """
        # Resolve safely — strip leading slash, prevent path traversal
        rel_path = rel_path.lstrip("/") or "index.html"
        candidate = (_DIST_ROOT / rel_path).resolve()
        # Refuse traversal outside dist/
        try:
            candidate.relative_to(_DIST_ROOT.resolve())
        except ValueError:
            self._not_found()
            return
        # SPA fallback: unknown paths → index.html
        if not candidate.exists() or candidate.is_dir():
            candidate = _DIST_ROOT / "index.html"
        if not candidate.exists():
            self._send_json({"error": "frontend not built — run npm run build"}, 503)
            return
        mime, _ = mimetypes.guess_type(str(candidate))
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ── CORS preflight ────────────────────────────────────────────────────
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/healthz":
            if self.server.ready_event.is_set():  # type: ignore[attr-defined]
                self.send_response(200)
                self._cors()
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(503)
                self._cors()
                self.end_headers()
                self.wfile.write(b"not ready")

        elif path == "/api/status":
            self._send_json(self._ov.get_status())

        elif path == "/api/scenes":
            self._send_json({"scenes": self._ov.list_scenes_api()})

        elif path == "/api/hierarchy":
            qs       = parse_qs(parsed.query)
            prim_pth = qs.get("path", ["/"])[0]
            self._send_json({
                "path":     prim_pth,
                "children": self._ov.get_hierarchy(prim_pth),
            })

        elif path == "/api/prim":
            qs       = parse_qs(parsed.query)
            prim_pth = qs.get("path", [""])[0]
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            self._send_json(self._ov.get_prim_properties(prim_pth))

        elif path == "/api/prim/variants":
            qs       = parse_qs(parsed.query)
            prim_pth = qs.get("path", [""])[0]
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            self._send_json(self._ov.get_prim_variants(prim_pth))

        elif path == "/api/prim/bbox":
            qs       = parse_qs(parsed.query)
            prim_pth = qs.get("path", [""])[0]
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            self._send_json(self._ov.get_prim_bbox(prim_pth))

        elif path == "/api/search":
            qs = parse_qs(parsed.query)
            q  = qs.get("q", [""])[0].strip()
            self._send_json({"results": self._ov.search_prims(q)})

        elif path == "/api/timeline":
            self._send_json(self._ov.get_timeline())

        elif path == "/api/render/mode":
            self._send_json({"mode": self._ov._render_mode})

        elif path == "/api/telemetry/prims":
            resolve_event = threading.Event()
            result_holder_tp: dict = {}
            self._cmd_q.put({
                "event_type": Events.TELEMETRY_DISCOVER,
                "payload": {
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_tp,
                },
            })
            if resolve_event.wait(timeout=15.0):
                if result_holder_tp.get("ok"):
                    self._send_json({"prims": result_holder_tp.get("prims", [])})
                else:
                    self._send_json({"error": result_holder_tp.get("error", "failed")}, 400)
            else:
                self._send_json({"error": "timeout"}, 504)

        elif path == "/api/snapshot":
            png = self._ov.get_snapshot_png()
            if png is None:
                self._send_json({"error": "no frame available — load a scene first"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Content-Disposition", 'attachment; filename="snapshot.png"')
            self._cors()
            self.end_headers()
            self.wfile.write(png)

        elif path == "/api/bookmarks":
            self._send_json({"bookmarks": self._ov.list_bookmarks()})

        elif path.startswith("/api/"):
            self._not_found()

        else:
            # Serve static frontend files (Vite dist/) — SPA fallback included
            self._serve_static(path)

    # ── POST ──────────────────────────────────────────────────────────────
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path   = parsed.path

        # ── Legacy /camera endpoint — keep unchanged ────────────────────────────────
        if path == "/camera":
            try:
                data = self._read_json()
                self._cmd_q.put(data)
                self._send_json({"ok": True})
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
            return

        # ── Scene interaction API ──────────────────────────────────────────────────
        if path == "/api/scene":
            data      = self._read_json()
            scene_pth = data.get("path", "")
            if not scene_pth:
                self._send_json({"error": "path required"}, 400)
                return
            self._cmd_q.put({
                "event_type": Events.OPEN_STAGE_REQUEST,
                "payload":    {"url": scene_pth},
            })
            self._send_json({"ok": True, "path": scene_pth})

        elif path == "/api/pick":
            # Blocking pick — waits up to 2 s for the render thread.
            # The render thread fills result_holder and signals resolve_event.
            data          = self._read_json()
            x             = int(data.get("x", 0))
            y             = int(data.get("y", 0))
            resolve_event = threading.Event()
            result_holder: dict = {}
            self._cmd_q.put({
                "event_type": Events.PICK_REQUEST,
                "payload": {
                    "x":              x,
                    "y":              y,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder,
                },
            })
            if resolve_event.wait(timeout=2.0):
                self._send_json({"prim_path": result_holder.get("prim_path")})
            else:
                self._send_json({"prim_path": None, "timeout": True})

        elif path == "/api/bookmarks":
            data = self._read_json()
            name = data.get("name", "").strip()
            if not name:
                self._send_json({"error": "name required"}, 400)
                return
            self._ov.save_bookmark(name)
            self._send_json({"ok": True, "name": name})

        elif path.startswith("/api/bookmarks/recall/"):
            name = path[len("/api/bookmarks/recall/"):]
            ok   = self._ov.recall_bookmark(name)
            self._send_json({"ok": ok})

        elif path == "/api/measure":
            data   = self._read_json()
            path_a = data.get("path_a", "")
            path_b = data.get("path_b", "")
            if not path_a or not path_b:
                self._send_json({"error": "path_a and path_b required"}, 400)
                return
            self._send_json(self._ov.measure_distance(path_a, path_b))

        elif path == "/api/timeline":
            data          = self._read_json()
            resolve_event = threading.Event()
            result_holder_t: dict = {}
            self._cmd_q.put({
                "event_type": Events.SET_TIMELINE,
                "payload": {**data,
                            "_resolve_event": resolve_event,
                            "_result_holder": result_holder_t},
            })
            resolve_event.wait(timeout=2.0)
            self._send_json(self._ov.get_timeline())

        elif path == "/api/render/mode":
            data          = self._read_json()
            mode          = data.get("mode", "rtx")
            resolve_event = threading.Event()
            result_holder_r: dict = {}
            self._cmd_q.put({
                "event_type": Events.SET_RENDER_MODE,
                "payload": {"mode": mode,
                            "_resolve_event": resolve_event,
                            "_result_holder": result_holder_r},
            })
            if resolve_event.wait(timeout=5.0):
                self._send_json({"ok": result_holder_r.get("ok", False), "mode": mode})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/prim/variant":
            data          = self._read_json()
            prim_pth      = data.get("path", "")
            variant_set   = data.get("variant_set", "")
            variant       = data.get("variant", "")
            if not all([prim_pth, variant_set, variant]):
                self._send_json({"error": "path, variant_set, variant required"}, 400)
                return
            resolve_event = threading.Event()
            result_holder_vr: dict = {}
            self._cmd_q.put({
                "event_type": Events.SET_VARIANT,
                "payload": {"path": prim_pth, "variant_set": variant_set,
                            "variant": variant,
                            "_resolve_event": resolve_event,
                            "_result_holder": result_holder_vr},
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_vr.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/prim/xform":
            # Set prim transform — write to USD and reload (blocking, ~2 s)
            data          = self._read_json()
            prim_pth      = data.get("path", "")
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            resolve_event = threading.Event()
            result_holder: dict = {}
            self._cmd_q.put({
                "event_type": Events.EDIT_PRIM,
                "payload": {
                    "path":          prim_pth,
                    "translate":     data.get("translate"),
                    "rotate":        data.get("rotate"),
                    "scale":         data.get("scale"),
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/prim/visibility":
            data          = self._read_json()
            prim_pth      = data.get("path", "")
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            visible       = bool(data.get("visible", True))
            resolve_event = threading.Event()
            result_holder_v: dict = {}
            self._cmd_q.put({
                "event_type": Events.EDIT_PRIM,
                "payload": {
                    "path":           prim_pth,
                    "visibility":     visible,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_v,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_v.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/save":
            data          = self._read_json()
            output_path   = data.get("output_path", "")
            if not output_path:
                self._send_json({"error": "output_path required"}, 400)
                return
            resolve_event = threading.Event()
            result_holder_s: dict = {}
            self._cmd_q.put({
                "event_type": Events.SAVE_STAGE,
                "payload": {
                    "output_path":    output_path,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_s,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_s.get("ok", False), "path": output_path})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        # ── Session layer authoring ──────────────────────────────────────────────────

        elif path == "/api/create_prim":
            data      = self._read_json()
            prim_type = data.get("type", "").strip()
            name      = data.get("name", "").strip()
            if not prim_type or not name:
                self._send_json({"error": "type and name required"}, 400)
                return
            resolve_event = threading.Event()
            result_holder_cp: dict = {}
            self._cmd_q.put({
                "event_type": Events.CREATE_PRIM,
                "payload": {
                    "type":           prim_type,
                    "name":           name,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_cp,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_cp.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/deactivate_prim":
            data     = self._read_json()
            prim_pth = data.get("path", "").strip()
            if not prim_pth:
                self._send_json({"error": "path required"}, 400)
                return
            resolve_event = threading.Event()
            result_holder_dp: dict = {}
            self._cmd_q.put({
                "event_type": Events.DEACTIVATE_PRIM,
                "payload": {
                    "path":           prim_pth,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_dp,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_dp.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/undo":
            resolve_event = threading.Event()
            result_holder_u: dict = {}
            self._cmd_q.put({
                "event_type": Events.UNDO,
                "payload": {
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_u,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_u.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/redo":
            resolve_event = threading.Event()
            result_holder_r2: dict = {}
            self._cmd_q.put({
                "event_type": Events.REDO,
                "payload": {
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_r2,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_r2.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/telemetry/generate":
            body     = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bindings = body.get("bindings", [])
            duration = float(body.get("duration", 30.0))
            fps      = float(body.get("fps", 24.0))
            resolve_event = threading.Event()
            result_holder_tg: dict = {}
            self._cmd_q.put({
                "event_type": Events.TELEMETRY_GENERATE,
                "payload": {
                    "bindings":       bindings,
                    "duration":       duration,
                    "fps":            fps,
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_tg,
                },
            })
            if resolve_event.wait(timeout=60.0):   # allow time for large clip gen
                self._send_json({
                    "ok":       result_holder_tg.get("ok", False),
                    "error":    result_holder_tg.get("error"),
                    "duration": duration,
                    "fps":      fps,
                })
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        elif path == "/api/telemetry/stop":
            resolve_event = threading.Event()
            result_holder_ts: dict = {}
            self._cmd_q.put({
                "event_type": Events.TELEMETRY_STOP,
                "payload": {
                    "_resolve_event": resolve_event,
                    "_result_holder": result_holder_ts,
                },
            })
            if resolve_event.wait(timeout=15.0):
                self._send_json({"ok": result_holder_ts.get("ok", False)})
            else:
                self._send_json({"ok": False, "error": "timeout"}, 504)

        else:
            self._not_found()

    # ── DELETE ────────────────────────────────────────────────────────────
    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/bookmarks/"):
            name = path[len("/api/bookmarks/"):]
            ok   = self._ov.delete_bookmark(name)
            self._send_json({"ok": ok})
        else:
            self._not_found()

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass  # silence per-request log noise


def _start_api_server(
    cfg: ServerConfig,
    ready: threading.Event,
    cmd_queue,
    ov_server: OVWebViewerServer,
) -> None:
    httpd = HTTPServer(("0.0.0.0", cfg.health_port), _ApiHandler)
    httpd.ready_event = ready      # type: ignore[attr-defined]
    httpd.cmd_queue   = cmd_queue  # type: ignore[attr-defined]
    httpd.ov_server   = ov_server  # type: ignore[attr-defined]
    t = threading.Thread(target=httpd.serve_forever, name="api-server", daemon=True)
    t.start()
    log.info("API server on :%d  (/healthz  /camera  /api/*)", cfg.health_port)


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    cfg = ServerConfig.from_args()
    log.info("ServerConfig: %s", cfg)

    server = OVWebViewerServer(cfg)

    def _shutdown(sig: int, _frame: object) -> None:
        log.info("Signal %d received — shutting down", sig)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _start_api_server(cfg, server.ready_event, server._cmd_q, server)
    server.start()

    # Keep the main thread alive (render loop runs in a daemon thread)
    try:
        signal.pause()
    except AttributeError:
        # signal.pause() not available on Windows
        while True:
            threading.Event().wait(timeout=1.0)


if __name__ == "__main__":
    main()
