# SPDX-License-Identifier: Apache-2.0
"""Main server runtime — ties renderer, scene loader, stream server together.

Canonical startup order (from streaming-server reference):
  1. OVRTX_SKIP_USD_CHECK=1   (set in __main__.py before any import)
  2. Construct ovrtx.Renderer
  3. warp.init()
  4. Load initial stage (if configured)
  5. Warm up renderer  (~90 s shader compile on L40S cold start)
  6. Initialize ovstream + register callbacks + start WebRTC server
  7. Start /healthz  (503 until first valid frame)
  8. Start render loop thread  (sole owner of renderer.step)

The render loop is the single owner of:
  renderer.step(), stage load/reset, write_attribute(), pick queries,
  selection outline writes, and stream_video().
All other threads enqueue work via _cmd_queue.
"""
from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from typing import Any

import numpy as np
import warp as wp

from .config import Events, ServerConfig
from .frame_converter import FrameConverter
from .renderer_runtime import RendererRuntime
from .scene_loader import SceneLoader, _DEFAULT_EYE, _DEFAULT_TARGET, _look_at_row_major
from .stream_server import StreamServer

log = logging.getLogger("viewer.server")


# ── Snapshot helpers ──────────────────────────────────────────────────────────

import struct as _struct
import zlib as _zlib


def _encode_png(rgb: np.ndarray) -> bytes:
    """Encode an (H, W, 3) uint8 RGB numpy array as a PNG byte string.

    Pure-Python implementation — no Pillow / OpenCV dependency.
    """
    h, w = rgb.shape[:2]
    # Each row: filter byte (0 = None) + raw RGB pixels
    rows = b"".join(b"\x00" + bytes(rgb[y].tobytes()) for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = _zlib.crc32(tag + data) & 0xFFFF_FFFF
        return _struct.pack(">I", len(data)) + tag + data + _struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", _struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", _zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


class OVWebViewerServer:
    """Top-level application runtime for the streaming USD viewer."""

    # Runtime state labels
    STARTING  = "starting"
    IDLE      = "idle"       # no stage loaded — waiting for openStageRequest
    LOADING   = "loading"
    STREAMING = "streaming"
    ERROR     = "error"
    STOPPING  = "stopping"

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg   = cfg
        self._state = self.STARTING
        self._ready = threading.Event()                 # set after first valid frame
        self._cmd_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop  = threading.Event()

        # Sub-systems
        self._renderer  = RendererRuntime(cfg)
        self._loader    = SceneLoader(cfg)
        self._stream    = StreamServer(cfg)
        self._converter = FrameConverter(cfg.width, cfg.height)

        # State cache for reconnecting clients
        self._current_stage: str | None = None

        # ── Spherical camera state ───────────────────────────────────────────────────
        # Spherical coords: azimuth (yaw around Y), elevation (pitch above XZ),
        # radius (distance from target).  Eye = target + (r·cos(el)·sin(az),
        # r·sin(el), r·cos(el)·cos(az)).
        # Initialised to the scene defaults; reset each time a stage is loaded.
        d = np.array(_DEFAULT_EYE, dtype=np.float64)
        self._cam_r:      float = float(np.linalg.norm(d))          # ~866 cm
        self._cam_el:     float = float(math.asin(d[1] / self._cam_r))  # ~35.26°
        self._cam_az:     float = float(math.atan2(d[0], d[2]))     # 45°
        self._cam_target: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._cam_dirty:  bool = False

        # ── NVST input tracking (server-side drag state) ──────────────────
        # NVST MOVE events always report button_state=UP even during a drag.
        # We track button hold state via BUTTON DOWN/UP events instead.
        # X11 button convention: 1=left (orbit), 3=right (pan).
        self._mouse_btn: int = 0   # 0=none; 1=left; 3=right
        self._mouse_x:   int = 0
        self._mouse_y:   int = 0
        # Click detection: if total movement since DOWN < threshold → pick
        self._click_start_x: int = 0
        self._click_start_y: int = 0
        _CLICK_PX = 5
        self._CLICK_THRESHOLD: int = _CLICK_PX

        # ── Pick & hierarchy state ────────────────────────────────────────────────
        self._current_stage_resolved: str | None = None  # abs path for pxr
        self._prim_count: int = 0
        self._last_picked_prim: str | None = None
        self._bookmarks: dict[str, dict] = {}
        # Cache the pxr Stage for hierarchy traversal so we don't reopen the
        # file on every GET /api/hierarchy call.  Invalidated when scene changes.
        # Tuple (resolved_path, stage) or None.  Read-only after write → GIL-safe.
        self._hierarchy_stage_cache: tuple | None = None
        # Pending pick request (render-thread-owned after drain).
        # Set by _handle_command(PICK_REQUEST); consumed in _run_render_loop
        # by enqueueing before step() and reading the result after step().
        self._pick_request: dict | None = None

        # ── Animation timeline state ─────────────────────────────────────────────────
        self._time_code:       float = 0.0
        self._time_start:      float = 0.0
        self._time_end:        float = 0.0
        self._stage_fps:       float = 24.0
        self._is_playing:      bool  = False
        self._playback_speed:  float = 1.0
        self._stage_has_anim:  bool  = False

        # ── Render mode state ───────────────────────────────────────────────────────
        self._render_mode: str = "rtx"   # "rtx" | "unlit" | "wireframe"

        # ── Snapshot capture ─────────────────────────────────────────────────────────
        self._snapshot_pending = threading.Event()
        self._snapshot_ready   = threading.Event()
        self._snapshot_frame: np.ndarray | None = None
        self._snapshot_lock = threading.Lock()

        # ── Telemetry simulation ─────────────────────────────────────────────────────
        self._telemetry_active:   bool       = False
        self._telemetry_bindings: list[dict] = []

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @property
    def ready_event(self) -> threading.Event:
        """Event set after the first valid BGRA frame — used by /healthz."""
        return self._ready

    def start(self) -> None:
        """Full startup sequence.  Blocks until the render loop is running."""
        log.info(
            "Starting OVWebViewerServer  %dx%d @ %dfps",
            self.cfg.width, self.cfg.height, self.cfg.target_fps,
        )

        # Step 2: construct ovrtx renderer
        self._renderer.construct()

        # Step 3: warp + BGRA buffer
        wp.init()
        self._converter.ensure_buffer()

        # Step 4: load initial stage (synchronous — before warmup)
        if self.cfg.initial_stage:
            self._load_stage_sync(self.cfg.initial_stage)
        else:
            self._state = self.IDLE
            log.info("No initial stage — waiting for openStageRequest")

        # Step 5: renderer warmup (compiles shaders; skip when no stage)
        if self._renderer.has_stage:
            log.info("Warming up renderer (shader compilation, ~90 s on cold L40S) ...")
            self._renderer.warmup(write_camera_xform=self._maybe_write_camera)
            log.info("Renderer warmup done")

        # Step 6: register callbacks and start ovstream
        self._stream.on_connection_cb = self._on_connection
        self._stream.on_message_cb    = self._on_message
        self._stream.on_input_cb      = self._on_input
        self._stream.start()

        # Mark ready — HTTP API and WebRTC signaling are live; a stage doesn't
        # need to be loaded yet.  healthz returns 200 from this point on.
        self._ready.set()
        log.info("Server ready (healthz → ok)")

        # Step 8: render loop
        t = threading.Thread(target=self._render_loop, name="render-loop", daemon=True)
        t.start()
        log.info("Render loop started")

    def stop(self) -> None:
        log.info("Stopping server ...")
        self._state = self.STOPPING
        self._stop.set()
        self._stream.stop()

    def get_snapshot_png(self) -> bytes | None:
        """Capture the current rendered frame and return it as PNG bytes.

        Signals the render loop to copy the next frame to CPU.
        Blocks up to 2 seconds for the frame to arrive.
        Returns None if no stage is loaded or capture times out.
        """
        if not self._renderer.has_stage:
            return None
        self._snapshot_ready.clear()
        self._snapshot_pending.set()
        if not self._snapshot_ready.wait(timeout=2.0):
            log.warning("get_snapshot_png: timed out waiting for frame")
            return None
        with self._snapshot_lock:
            frame = self._snapshot_frame
            self._snapshot_frame = None
        if frame is None:
            return None
        return _encode_png(frame)

    # ------------------------------------------------------------------ #
    # Stage loading (render-thread or startup only)
    # ------------------------------------------------------------------ #
    def _load_stage_sync(self, stage_path: str) -> bool:
        """Resolve, build inline root, and open the stage.

        Must be called from the render thread (or before the render loop starts).
        Returns True on success.
        """
        self._state = self.LOADING
        self._current_stage = stage_path

        resolved = self._loader.resolve(stage_path)
        if resolved is None:
            log.error("Stage not found: %s", stage_path)
            self._state = self.ERROR
            return False

        try:
            self._loader.reset_session()   # clear any authored prims / overrides
            usda = self._loader.build_inline_root(resolved)
            self._renderer.open_inline_root(usda)
            self._init_camera_state()   # reset spherical state → marks _cam_dirty
            self._current_stage_resolved = str(resolved)
            self._hierarchy_stage_cache  = None   # invalidate on scene change
            self._prim_count = self._count_stage_prims(str(resolved))
            self._last_picked_prim = None
            self._detect_animation(str(resolved))   # populate timeline info
            self._state = self.STREAMING
            log.info("Stage loaded: %s  (%d prims)", resolved, self._prim_count)
            return True
        except Exception as exc:
            log.exception("Failed to load stage: %s", stage_path)
            self._state = self.ERROR
            self._stream.send_event(Events.VIEWER_ERROR, {"message": str(exc)})
            return False

    # ------------------------------------------------------------------ #
    # Render loop  (sole owner of renderer.step and stream_video)
    # ------------------------------------------------------------------ #
    def _render_loop(self) -> None:
        import ovrtx  # already imported by RendererRuntime; safe to re-import

        dt          = 1.0 / float(self.cfg.target_fps)
        first_frame = True
        rp_path     = self._renderer.render_product_path
        raw_renderer = self._renderer.renderer  # ovrtx.Renderer instance

        log.info("Render loop thread running (dt=%.4f s)", dt)
        try:
            self._run_render_loop(dt, rp_path, raw_renderer)
        except Exception:
            log.exception("RENDER LOOP CRASHED — streaming has stopped")

    def _run_render_loop(self, dt: float, rp_path: str, raw_renderer) -> None:
        first_frame = True
        while not self._stop.is_set():
            t0 = time.monotonic()

            # Drain command queue (stage loads, camera commands, etc.)
            self._drain_commands()

            # Flush any pending camera update (set by orbit/pan/zoom commands)
            try:
                self._maybe_write_camera()
            except Exception:
                log.exception("_maybe_write_camera() raised — camera update skipped")
                self._cam_dirty = False  # prevent repeat crash every frame

            # Skip frame if no stage is loaded
            if not self._renderer.has_stage:
                time.sleep(0.05)
                continue

            # ── Enqueue pick query before step (result arrives after step) ─
            pending_pick = self._pick_request
            if pending_pick is not None:
                self._pick_request = None  # consume
                self._renderer.enqueue_pick(
                    int(pending_pick.get("x", 0)),
                    int(pending_pick.get("y", 0)),
                )

            # ── Advance animation time code ──────────────────────────────
            if self._is_playing and self._stage_has_anim:
                self._time_code += dt * self._playback_speed * self._stage_fps
                if self._time_code > self._time_end:
                    self._time_code = self._time_start   # loop

            # ── Step renderer ────────────────────────────────────────────
            try:
                if self._stage_has_anim:
                    try:
                        outputs = raw_renderer.step(
                            render_products={rp_path},
                            delta_time=dt,
                            time_code=self._time_code,
                        )
                    except TypeError:
                        # ovrtx version doesn't support time_code kwarg
                        outputs = raw_renderer.step(
                            render_products={rp_path},
                            delta_time=dt,
                        )
                else:
                    outputs = raw_renderer.step(
                        render_products={rp_path},
                        delta_time=dt,
                    )
            except Exception:
                log.exception("renderer.step() failed")
                time.sleep(0.1)
                continue

            # ── Read pick result (if a query was enqueued this frame) ─────
            if pending_pick is not None:
                prim_path = self._renderer.read_pick_result(outputs)
                self._last_picked_prim = prim_path
                log.info("PICK (%d, %d) → %s",
                         pending_pick.get("x"), pending_pick.get("y"), prim_path)
                resolve_event = pending_pick.get("_resolve_event")
                result_holder = pending_pick.get("_result_holder")
                if resolve_event is not None and result_holder is not None:
                    result_holder["prim_path"] = prim_path
                    resolve_event.set()

            # ── Extract LdrColor and convert RGBA → BGRA on CUDA ─────────
            try:
                fout    = outputs[rp_path].frames[0]
                ldr_var = fout.render_vars["LdrColor"]

                # map() with device="cuda" keeps the buffer on the GPU.
                # Falls back to default map (may be CPU DLPack) if kwarg unsupported.
                try:
                    ctx = ldr_var.map(device="cuda")
                except TypeError:
                    ctx = ldr_var.map()

                with ctx as mapped:
                    bgra = self._converter.rgba_to_bgra(mapped)

                if first_frame:
                    log.info(
                        "First BGRA frame ready: %dx%d", self.cfg.width, self.cfg.height
                    )
                    first_frame = False

                self._stream.stream_video(bgra)

                # ── Snapshot capture (on demand only) ────────────────────
                if self._snapshot_pending.is_set():
                    self._snapshot_pending.clear()
                    try:
                        # bgra is a warp CUDA array (H,W,4) uint8, channels BGRA.
                        # .numpy() copies to CPU; swap B↔R to get RGB for PNG.
                        bgra_np = bgra.numpy()               # (H, W, 4) uint8 BGRA
                        rgb_np  = bgra_np[:, :, [2, 1, 0]]  # BGRA → RGB, drop A
                        with self._snapshot_lock:
                            self._snapshot_frame = rgb_np.copy()
                        self._snapshot_ready.set()
                    except Exception:
                        log.warning("Snapshot capture failed", exc_info=True)

            except Exception:
                log.error("Frame extraction/conversion failed", exc_info=True)

            # ── Pace to target FPS ────────────────────────────────────────
            elapsed = time.monotonic() - t0
            slack   = dt - elapsed
            if slack > 0:
                time.sleep(slack)

    # ------------------------------------------------------------------ #
    # Command handling  (called from render thread via _drain_commands)
    # ------------------------------------------------------------------ #
    def _drain_commands(self) -> None:
        try:
            while True:
                self._handle_command(self._cmd_q.get_nowait())
        except queue.Empty:
            pass

    def _handle_command(self, cmd: dict) -> None:
        event   = cmd.get("event_type", "")
        payload = cmd.get("payload", {})

        if event == Events.OPEN_STAGE_REQUEST:
            url = payload.get("url") or payload.get("path", "")
            ok  = self._load_stage_sync(url)
            self._stream.send_event(
                Events.OPEN_STAGE_RESULT,
                {"result": "ok" if ok else "error", "url": url},
            )

        elif event == Events.RESET_STAGE_REQUEST:
            self._renderer.reset_stage()
            self._current_stage = None
            self._state = self.IDLE

        elif event == Events.LOADING_STATE_QUERY:
            self._stream.send_event(
                Events.LOADING_STATE_RESPONSE,
                {"state": self._state, "stage": self._current_stage},
            )

        elif event == Events.LIST_SCENES_REQUEST:
            self._stream.send_event(
                Events.LIST_SCENES_RESULT,
                {"scenes": self._list_local_scenes()},
            )

        elif event == Events.CAMERA_ORBIT:
            dx, dy = float(payload.get("dx", 0)), float(payload.get("dy", 0))
            log.info("CAM orbit  dx=%.1f dy=%.1f  az=%.3f el=%.3f r=%.1f",
                     dx, dy, self._cam_az, self._cam_el, self._cam_r)
            self._apply_orbit(dx, dy)

        elif event == Events.CAMERA_PAN:
            dx, dy = float(payload.get("dx", 0)), float(payload.get("dy", 0))
            log.info("CAM pan    dx=%.1f dy=%.1f", dx, dy)
            self._apply_pan(dx, dy)

        elif event == Events.CAMERA_ZOOM:
            delta = float(payload.get("delta", 0))
            log.info("CAM zoom   delta=%.1f  r=%.1f", delta, self._cam_r)
            self._apply_zoom(delta)

        elif event == Events.FIT_CAMERA_REQUEST:
            self._init_camera_state()

        elif event == Events.CAMERA_COMMAND_REQUEST:
            log.debug("cameraCommand not yet implemented")

        elif event == Events.PICK_REQUEST:
            # Store the pick request — it is consumed in _run_render_loop:
            # enqueue_pick() is called before step(), read_pick_result() after.
            # If multiple picks arrive before the next frame, last one wins.
            self._pick_request = payload

        elif event == Events.EDIT_PRIM:
            # Write prim edit to USD file and reload the stage.
            # The HTTP handler waits on _resolve_event for up to 10 s.
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            ok = self._write_prim_edit(payload)
            result_holder["ok"] = ok
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.SAVE_STAGE:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            output_path   = payload.get("output_path", "")
            ok = self.save_stage_copy(output_path) if output_path else False
            result_holder["ok"] = ok
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.SET_VARIANT:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            ok = self._apply_variant(
                payload.get("path", ""),
                payload.get("variant_set", ""),
                payload.get("variant", ""),
            )
            result_holder["ok"] = ok
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.SET_TIMELINE:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            if "time_code" in payload:
                self._time_code = max(self._time_start,
                                      min(self._time_end, float(payload["time_code"])))
            if "playing" in payload:
                self._is_playing = bool(payload["playing"])
            if "speed" in payload:
                self._playback_speed = max(0.1, float(payload["speed"]))
            result_holder["ok"] = True
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.SET_RENDER_MODE:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            ok = self._apply_render_mode(payload.get("mode", "rtx"))
            result_holder["ok"] = ok
            if resolve_event is not None:
                resolve_event.set()

        # ── Session layer authoring ──────────────────────────────────────────────────

        elif event == Events.CREATE_PRIM:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            usda = self._loader.create_prim(
                payload.get("type", ""),
                payload.get("name", ""),
            )
            if usda is not None:
                try:
                    self._renderer.open_inline_root(usda)
                    result_holder["ok"] = True
                except Exception as exc:
                    log.warning("CREATE_PRIM reload failed: %s", exc)
                    result_holder["ok"] = False
            else:
                result_holder["ok"] = False
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.DEACTIVATE_PRIM:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            usda = self._loader.deactivate_prim(payload.get("path", ""))
            if usda is not None:
                try:
                    self._renderer.open_inline_root(usda)
                    result_holder["ok"] = True
                except Exception as exc:
                    log.warning("DEACTIVATE_PRIM reload failed: %s", exc)
                    result_holder["ok"] = False
            else:
                result_holder["ok"] = False
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.UNDO:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            usda = self._loader.undo()
            if usda is not None:
                try:
                    self._renderer.open_inline_root(usda)
                    result_holder["ok"] = True
                except Exception as exc:
                    log.warning("UNDO reload failed: %s", exc)
                    result_holder["ok"] = False
            else:
                result_holder["ok"] = False  # nothing to undo
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.REDO:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            usda = self._loader.redo()
            if usda is not None:
                try:
                    self._renderer.open_inline_root(usda)
                    result_holder["ok"] = True
                except Exception as exc:
                    log.warning("REDO reload failed: %s", exc)
                    result_holder["ok"] = False
            else:
                result_holder["ok"] = False  # nothing to redo
            if resolve_event is not None:
                resolve_event.set()

        # ── Telemetry simulation ─────────────────────────────────────────────────────

        elif event == Events.TELEMETRY_DISCOVER:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            resolved = self._current_stage_resolved
            if resolved:
                prims = self._loader.discover_xformable_prims(resolved)
                result_holder["prims"] = prims
                result_holder["ok"]    = True
            else:
                result_holder["prims"] = []
                result_holder["ok"]    = False
                result_holder["error"] = "No scene loaded"
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.TELEMETRY_GENERATE:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            resolved  = self._current_stage_resolved
            bindings  = payload.get("bindings", [])
            duration  = float(payload.get("duration", 30.0))
            fps       = float(payload.get("fps", 24.0))
            if not resolved:
                result_holder["ok"]    = False
                result_holder["error"] = "No scene loaded"
            elif not bindings:
                result_holder["ok"]    = False
                result_holder["error"] = "No bindings provided"
            else:
                try:
                    usda = self._loader.generate_telemetry_usda(
                        resolved, bindings, duration=duration, fps=fps
                    )
                    self._renderer.open_inline_root(usda)
                    # Start timeline playing on loop
                    self._time_start    = 0.0
                    self._time_end      = duration * fps - 1
                    self._stage_fps     = fps
                    self._time_code     = 0.0
                    self._is_playing    = True
                    self._stage_has_anim = True
                    self._telemetry_active   = True
                    self._telemetry_bindings = bindings
                    result_holder["ok"]      = True
                    log.info("Telemetry generated: %d bindings, %.1fs @%.0ffps",
                             len(bindings), duration, fps)
                    # Drive Kit's native timeline so the renderer evaluates the correct
                    # USD time code each frame.  ovrtx.step() ignores our time_code
                    # kwarg, but it DOES honour the Kit timeline interface.
                    try:
                        import omni.timeline  # type: ignore
                        tl = omni.timeline.get_timeline_interface()
                        tl.set_time_codes_per_second(fps)
                        tl.set_start_time(0.0)          # seconds
                        tl.set_end_time(duration)        # seconds
                        tl.set_looping(True)
                        tl.play()
                        log.info("Kit timeline: play() — %.1fs loop @ %.0f fps", duration, fps)
                    except Exception as _tl_exc:
                        log.warning("omni.timeline unavailable (%s) — relying on manual time_code", _tl_exc)
                except Exception as exc:
                    log.warning("TELEMETRY_GENERATE failed: %s", exc)
                    result_holder["ok"]    = False
                    result_holder["error"] = str(exc)
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.TELEMETRY_STOP:
            resolve_event = payload.get("_resolve_event")
            result_holder = payload.get("_result_holder", {})
            self._telemetry_active   = False
            self._telemetry_bindings = []
            self._is_playing         = False
            self._stage_has_anim     = False
            # Stop Kit's native timeline
            try:
                import omni.timeline  # type: ignore
                omni.timeline.get_timeline_interface().stop()
            except Exception:
                pass
            # Reload the original scene (without telemetry USDA)
            if self._current_stage_resolved:
                try:
                    usda = self._loader.build_inline_root(self._current_stage_resolved)
                    self._renderer.open_inline_root(usda)
                    result_holder["ok"] = True
                    log.info("Telemetry stopped — original scene reloaded")
                except Exception as exc:
                    log.warning("TELEMETRY_STOP reload failed: %s", exc)
                    result_holder["ok"]    = False
                    result_holder["error"] = str(exc)
            else:
                result_holder["ok"] = True
            if resolve_event is not None:
                resolve_event.set()

        elif event == Events.RECALL_BOOKMARK:
            az = float(payload.get("az", self._cam_az))
            el = float(payload.get("el", self._cam_el))
            r  = float(payload.get("r",  self._cam_r))
            t  = payload.get("target", list(self._cam_target))
            self._cam_az     = az
            self._cam_el     = el
            self._cam_r      = max(1.0, r)
            self._cam_target = (float(t[0]), float(t[1]), float(t[2]))
            self._cam_dirty  = True
            log.info("BOOKMARK recalled  az=%.2f el=%.2f r=%.1f", az, el, r)

        else:
            log.debug("Unhandled event: %s", event)

    def _list_local_scenes(self) -> list[str]:
        try:
            root = self.cfg.asset_root
            if root.exists():
                return sorted(
                    str(p.relative_to(root)) for p in root.rglob("*.usd*")
                )
        except Exception:
            pass
        return []

    def list_scenes_api(self) -> list[dict]:
        """Return scenes as {name, path} dicts for the /api/scenes endpoint."""
        root = self.cfg.asset_root
        results = []
        try:
            if root.exists():
                for p in sorted(root.rglob("*.usd*")):
                    results.append({"name": p.name, "path": str(p)})
        except Exception:
            pass
        return results

    # ------------------------------------------------------------------ #
    # Public API  (called from HTTP handler threads — read-only
    # state access is safe under CPython's GIL for simple attribute reads)
    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        """Thread-safe snapshot of server state for GET /api/status."""
        return {
            "state":            self._state,
            "scene":            self._current_stage,
            "prim_count":       self._prim_count,
            "cam_az_deg":       round(math.degrees(self._cam_az), 1),
            "cam_el_deg":       round(math.degrees(self._cam_el), 1),
            "cam_r":            round(self._cam_r, 1),
            "cam_target":       [round(v, 1) for v in self._cam_target],
            "last_picked_prim": self._last_picked_prim,
        }

    def get_hierarchy(self, prim_path: str = "/") -> list[dict]:
        """Return immediate children of prim_path via a read-only pxr traversal.

        The pxr Stage is opened once per loaded scene and cached so that
        repeated GET /api/hierarchy calls (e.g. tree expansion clicks) don't
        re-parse the file each time.  Cache is invalidated in _load_stage_sync.

        Safe to call from any HTTP handler thread — read-only Stage access
        under CPython's GIL is safe for simultaneous reads.
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return []
        try:
            from pxr import Usd  # pxr ships with ovrtx; safe to import here

            # Use cached stage if path hasn't changed
            cache = self._hierarchy_stage_cache
            if cache is not None and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            # "/" is the pseudo-root — GetPrimAtPath("/").IsValid() returns
            # False in pxr, so use GetPseudoRoot() directly for the root query.
            if prim_path in ("/", ""):
                prim = stage.GetPseudoRoot()
            else:
                prim = stage.GetPrimAtPath(prim_path)
                if not prim.IsValid():
                    return []
            return [
                {
                    "path":         str(child.GetPath()),
                    "name":         child.GetName(),
                    "type":         child.GetTypeName() or "def",
                    "has_children": bool(child.GetChildren()),
                }
                for child in prim.GetChildren()
            ]
        except Exception:
            log.debug("get_hierarchy(%s) error", prim_path, exc_info=True)
            return []

    def save_bookmark(self, name: str) -> None:
        """Save current camera spherical state as a named bookmark."""
        self._bookmarks[name] = {
            "az":     self._cam_az,
            "el":     self._cam_el,
            "r":      self._cam_r,
            "target": list(self._cam_target),
        }
        log.info("Bookmark saved: %r", name)

    def delete_bookmark(self, name: str) -> bool:
        if name in self._bookmarks:
            del self._bookmarks[name]
            log.info("Bookmark deleted: %r", name)
            return True
        return False

    def recall_bookmark(self, name: str) -> bool:
        """Enqueue a recall command for the render thread."""
        b = self._bookmarks.get(name)
        if b is None:
            return False
        self._cmd_q.put({
            "event_type": Events.RECALL_BOOKMARK,
            "payload":    b,
        })
        return True

    def list_bookmarks(self) -> list[dict]:
        """Return bookmark names and their camera state for GET /api/bookmarks."""
        return [
            {
                "name":       k,
                "az_deg":     round(math.degrees(v["az"]), 1),
                "el_deg":     round(math.degrees(v["el"]), 1),
                "r":          round(v["r"], 1),
            }
            for k, v in self._bookmarks.items()
        ]

    # ------------------------------------------------------------------ #
    # Prim property read / write  (HTTP handler threads for reads;
    # render thread for writes via EDIT_PRIM / SAVE_STAGE events)
    # ------------------------------------------------------------------ #

    def get_prim_properties(self, prim_path: str) -> dict:
        """Read prim type, attributes, xform ops, and visibility via pxr.

        Called from HTTP handler threads.  Uses the hierarchy stage cache
        (opened read-only) — safe under CPython GIL for concurrent reads.
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return {"error": "no stage loaded"}
        try:
            from pxr import Usd, UsdGeom

            cache = self._hierarchy_stage_cache
            if cache and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                # Fall back to session layer (prims created via session layer authoring)
                info = self._loader.get_session_prim_info(prim_path)
                if info:
                    return {
                        "path":       prim_path,
                        "type":       info["type"],
                        "translate":  info["translate"],
                        "rotate":     [0.0, 0.0, 0.0],
                        "scale":      [1.0, 1.0, 1.0],
                        "visibility": "inherited",
                        "has_xform":  info["type"] != "DomeLight",
                        "attrs":      [],
                    }
                return {"error": f"prim not found: {prim_path}"}

            result: dict = {
                "path":       prim_path,
                "type":       prim.GetTypeName() or "def",
                "translate":  [0.0, 0.0, 0.0],
                "rotate":     [0.0, 0.0, 0.0],
                "scale":      [1.0, 1.0, 1.0],
                "visibility": "inherited",
                "has_xform":  False,
                "attrs":      [],
            }

            # ── xform ops ────────────────────────────────────────────────
            if prim.IsA(UsdGeom.Xformable):
                result["has_xform"] = True
                xformable = UsdGeom.Xformable(prim)
                for op in xformable.GetOrderedXformOps():
                    name = op.GetName()
                    val  = op.Get()
                    if val is None:
                        continue
                    if "translate" in name:
                        result["translate"] = [round(float(v), 4) for v in val]
                    elif "rotateXYZ" in name or "rotateX" in name:
                        result["rotate"] = [round(float(v), 4) for v in val]
                    elif "scale" in name:
                        result["scale"] = [round(float(v), 4) for v in val]

            # ── visibility ────────────────────────────────────────────────
            if prim.IsA(UsdGeom.Imageable):
                imageable = UsdGeom.Imageable(prim)
                vis_attr  = imageable.GetVisibilityAttr()
                if vis_attr and vis_attr.HasAuthoredValue():
                    val = vis_attr.Get()
                    result["visibility"] = str(val) if val is not None else "inherited"

            # ── authored attributes (limited to readable scalar/vec types) ─
            attrs = []
            for attr in prim.GetAuthoredAttributes():
                try:
                    val = attr.Get()
                    if val is None:
                        continue
                    # Skip large arrays and binary data
                    type_name = str(attr.GetTypeName())
                    if any(t in type_name for t in ("[]", "Asset", "token[]")):
                        continue
                    attrs.append({
                        "name":  attr.GetName(),
                        "type":  type_name,
                        "value": str(val)[:120],
                    })
                except Exception:
                    pass
            result["attrs"] = attrs
            return result

        except Exception as exc:
            log.debug("get_prim_properties(%s) error", prim_path, exc_info=True)
            return {"error": str(exc)}

    def _write_prim_edit(self, payload: dict) -> bool:
        """Apply transform / visibility edits to a prim in the USD file, then reload.

        Render-thread-only.  Opens the user stage with pxr, authors the edits
        on the root layer, saves, then reloads the stage in ovrtx.
        The brief black screen during reload is expected behaviour.
        """
        prim_path  = payload.get("path", "")
        translate  = payload.get("translate")   # [x, y, z] or None
        rotate     = payload.get("rotate")       # [x, y, z] degrees or None
        scale      = payload.get("scale")        # [x, y, z] or None
        visibility = payload.get("visibility")   # True/False or None
        output_path = payload.get("output_path") # save-as path or None

        resolved = self._current_stage_resolved
        if not resolved:
            log.warning("_write_prim_edit: no resolved stage path")
            return False

        # ── Session prim? Route to session layer instead of USD file ─────────
        if translate is not None and self._loader.get_session_prim_info(prim_path):
            usda = self._loader.set_session_prim_xform(prim_path, translate)
            if usda:
                try:
                    self._renderer.open_inline_root(usda)
                except Exception as exc:
                    log.warning("session xform reload failed: %s", exc)
                    return False
            return usda is not None

        try:
            from pxr import Usd, UsdGeom, Gf

            # Open the actual user USD file for editing (not the inline root).
            stage = Usd.Stage.Open(resolved)
            prim  = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                log.warning("_write_prim_edit: prim not found: %s", prim_path)
                return False

            # ── xform edits ───────────────────────────────────────────────
            if prim.IsA(UsdGeom.Xformable) and any(
                v is not None for v in (translate, rotate, scale)
            ):
                xformable = UsdGeom.Xformable(prim)
                # Index existing ops by name for in-place updates
                existing  = {op.GetName(): op for op in xformable.GetOrderedXformOps()}

                if translate is not None:
                    op = existing.get("xformOp:translate") or xformable.AddTranslateOp()
                    op.Set(Gf.Vec3d(*[float(v) for v in translate]))

                if rotate is not None:
                    op = existing.get("xformOp:rotateXYZ") or xformable.AddRotateXYZOp()
                    op.Set(Gf.Vec3f(*[float(v) for v in rotate]))

                if scale is not None:
                    op = existing.get("xformOp:scale") or xformable.AddScaleOp()
                    op.Set(Gf.Vec3f(*[float(v) for v in scale]))

            # ── visibility edit ───────────────────────────────────────────
            if visibility is not None and prim.IsA(UsdGeom.Imageable):
                imageable = UsdGeom.Imageable(prim)
                if visibility:
                    imageable.MakeVisible()
                else:
                    imageable.MakeInvisible()

            # ── save ──────────────────────────────────────────────────────
            save_target = output_path or resolved
            if output_path:
                stage.Export(output_path)
                log.info("Stage saved to: %s", output_path)
            else:
                stage.Save()
                log.info("Stage saved: %s", resolved)

            # ── reload renderer ───────────────────────────────────────────
            # Invalidate caches; reload so ovrtx picks up the new USD data.
            self._hierarchy_stage_cache = None
            ok = self._load_stage_sync(self._current_stage)
            return ok

        except Exception:
            log.exception("_write_prim_edit(%s) failed", prim_path)
            return False

    def save_stage_copy(self, output_path: str) -> bool:
        """Export the current stage to a new file (no prim edits).

        Render-thread-only (called via SAVE_STAGE event).
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return False
        try:
            from pxr import Usd
            stage = Usd.Stage.Open(resolved)
            stage.Export(output_path)
            log.info("Stage exported to: %s", output_path)
            return True
        except Exception:
            log.exception("save_stage_copy(%s) failed", output_path)
            return False

    # ------------------------------------------------------------------ #
    # Search, variants, bbox, measure, timeline, render mode
    # ------------------------------------------------------------------ #

    def search_prims(self, query: str) -> list[dict]:
        """Traverse the stage and return prims whose name or type matches query.

        Case-insensitive substring match.  Called from HTTP handler threads.
        """
        resolved = self._current_stage_resolved
        if not resolved or not query:
            return []
        try:
            from pxr import Usd

            cache = self._hierarchy_stage_cache
            if cache and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            q = query.lower()
            results = []
            for prim in stage.TraverseAll():
                name = prim.GetName().lower()
                typ  = prim.GetTypeName().lower()
                if q in name or q in typ:
                    parent = prim.GetParent()
                    results.append({
                        "path":         str(prim.GetPath()),
                        "name":         prim.GetName(),
                        "type":         prim.GetTypeName() or "def",
                        "has_children": bool(prim.GetChildren()),
                        "parent":       str(parent.GetPath()) if parent else "/",
                    })
                    if len(results) >= 50:   # cap at 50 hits
                        break
            return results
        except Exception:
            log.debug("search_prims(%r) error", query, exc_info=True)
            return []

    def get_prim_variants(self, prim_path: str) -> dict:
        """Return variant sets and their choices for a given prim.

        Returns: {variant_sets: [{name, choices: [...], current: str}]}
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return {"variant_sets": []}
        try:
            from pxr import Usd

            cache = self._hierarchy_stage_cache
            if cache and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                return {"variant_sets": []}

            vsets = prim.GetVariantSets()
            result = []
            for vs_name in vsets.GetNames():
                vs      = vsets.GetVariantSet(vs_name)
                choices = vs.GetVariantNames()
                current = vs.GetVariantSelection()
                result.append({"name": vs_name, "choices": choices, "current": current})
            return {"variant_sets": result}
        except Exception:
            log.debug("get_prim_variants(%s) error", prim_path, exc_info=True)
            return {"variant_sets": []}

    def _apply_variant(self, prim_path: str, variant_set: str, variant: str) -> bool:
        """Set a variant selection on the USD file and reload.  Render-thread-only."""
        resolved = self._current_stage_resolved
        if not resolved:
            return False
        try:
            from pxr import Usd

            stage = Usd.Stage.Open(resolved)
            prim  = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                return False

            vs = prim.GetVariantSets().GetVariantSet(variant_set)
            vs.SetVariantSelection(variant)
            stage.Save()
            log.info("Variant set: %s.%s = %s", prim_path, variant_set, variant)

            self._hierarchy_stage_cache = None
            return self._load_stage_sync(self._current_stage)
        except Exception:
            log.exception("_apply_variant(%s, %s, %s) failed", prim_path, variant_set, variant)
            return False

    def get_prim_bbox(self, prim_path: str) -> dict:
        """Return world-space bounding box for a prim (center, min, max, dimensions).

        Called from HTTP handler threads — read-only pxr access.
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return {"error": "no stage"}
        try:
            from pxr import Usd, UsdGeom, Gf

            cache = self._hierarchy_stage_cache
            if cache and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            prim = stage.GetPrimAtPath(prim_path)
            if not prim.IsValid():
                return {"error": "prim not found"}

            purposes = [UsdGeom.Tokens.default_]
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), purposes, useExtentsHint=True
            )
            bbox = bbox_cache.ComputeWorldBound(prim)
            rng  = bbox.GetRange()

            if rng.IsEmpty():
                return {"error": "empty bounding box"}

            mn  = rng.GetMin()
            mx  = rng.GetMax()
            ctr = rng.GetMidpoint()
            sz  = mx - mn

            def r3(v): return [round(float(v[0]), 2), round(float(v[1]), 2), round(float(v[2]), 2)]

            return {
                "center":     r3(ctr),
                "min":        r3(mn),
                "max":        r3(mx),
                "dimensions": r3(sz),
            }
        except Exception as exc:
            log.debug("get_prim_bbox(%s) error", prim_path, exc_info=True)
            return {"error": str(exc)}

    def measure_distance(self, path_a: str, path_b: str) -> dict:
        """Return world-space distance between two prims.

        Strategy (in order):
          1. BBoxCache world bound midpoint — most accurate when geometry resolves.
          2. XformCache world translation — reliable fallback when references
             don't load (plain pxr can't resolve NVIDIA-specific USD plugins),
             producing a degenerate bbox at origin.
        """
        resolved = self._current_stage_resolved
        if not resolved:
            return {"error": "no stage"}
        try:
            import math as _math
            from pxr import Usd, UsdGeom, Gf

            cache = self._hierarchy_stage_cache
            if cache and cache[0] == resolved:
                stage = cache[1]
            else:
                stage = Usd.Stage.Open(resolved)
                self._hierarchy_stage_cache = (resolved, stage)

            purposes   = [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy, UsdGeom.Tokens.render]
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), purposes, useExtentsHint=True
            )
            xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())

            def world_center(path: str):
                prim = stage.GetPrimAtPath(path)
                if not prim.IsValid():
                    return None
                # Try bbox first — works when geometry is fully resolved
                try:
                    bbox = bbox_cache.ComputeWorldBound(prim)
                    rng  = bbox.GetRange()
                    if not rng.IsEmpty():
                        mid = rng.GetMidpoint()
                        sz  = rng.GetMax() - rng.GetMin()
                        # Only trust bbox if it has real volume (not a degenerate point)
                        if max(abs(sz[0]), abs(sz[1]), abs(sz[2])) > 0.001:
                            return mid
                except Exception:
                    pass
                # Fallback: world-space translation from the prim's xform
                try:
                    mat, _ = xform_cache.GetLocalToWorldTransform(prim)
                    t = mat.ExtractTranslation()
                    return Gf.Vec3d(t[0], t[1], t[2])
                except Exception:
                    pass
                return Gf.Vec3d(0, 0, 0)

            ca = world_center(path_a)
            cb = world_center(path_b)
            if ca is None or cb is None:
                return {"error": "could not compute position for one or both prims"}

            diff = cb - ca
            dist = _math.sqrt(diff[0]**2 + diff[1]**2 + diff[2]**2)
            def r3(v): return [round(float(v[0]), 2), round(float(v[1]), 2), round(float(v[2]), 2)]
            log.info("measure_distance: A=%s center=%s  B=%s center=%s  dist=%.2f",
                     path_a, r3(ca), path_b, r3(cb), dist)
            return {
                "distance":  round(dist, 2),
                "center_a":  r3(ca),
                "center_b":  r3(cb),
                "unit":      "cm",
            }
        except Exception as exc:
            log.warning("measure_distance(%s, %s) error: %s", path_a, path_b, exc, exc_info=True)
            return {"error": str(exc)}

    def get_timeline(self) -> dict:
        """Thread-safe snapshot of timeline state for GET /api/timeline."""
        return {
            "time_code":  round(self._time_code, 2),
            "time_start": self._time_start,
            "time_end":   self._time_end,
            "stage_fps":  self._stage_fps,
            "is_playing": self._is_playing,
            "speed":      self._playback_speed,
            "has_anim":   self._stage_has_anim,
        }

    def _detect_animation(self, resolved_path: str) -> None:
        """Detect animation range from the USD stage.  Render-thread-only (at load)."""
        try:
            from pxr import Usd
            stage = Usd.Stage.Open(resolved_path)
            start = stage.GetStartTimeCode()
            end   = stage.GetEndTimeCode()
            fps   = stage.GetTimeCodesPerSecond()
            has   = end > start
            self._time_start     = float(start)
            self._time_end       = float(end)
            self._stage_fps      = float(fps) if fps > 0 else 24.0
            self._time_code      = float(start)
            self._is_playing     = False
            self._stage_has_anim = has
            if has:
                log.info("Animation detected: %.0f – %.0f @ %.0f fps", start, end, fps)
        except Exception:
            self._stage_has_anim = False
            log.debug("_detect_animation() error", exc_info=True)

    def _apply_render_mode(self, mode: str) -> bool:
        """Set the render mode by writing to the RenderSettings prim.  Render-thread-only.

        Modes: "rtx" (full path-traced), "unlit" (no shadows/reflections), "wireframe".
        Uses renderer.write_attribute() on the RenderSettings prim.
        Falls back gracefully if the prim path or attribute isn't supported.
        """
        MODE_MAP = {
            "rtx":       "RaytracedLighting",
            "unlit":     "Unlit",
            "wireframe": "Wireframe",
        }
        render_mode_str = MODE_MAP.get(mode, "RaytracedLighting")

        # carb and write_attribute don't work for string/token attributes.
        # The reliable approach: author `token renderMode` in the inline USDA
        # root and reload the stage.  Same ~2 s blink as other USD edits.
        resolved = self._current_stage_resolved
        if not resolved:
            log.warning("_apply_render_mode: no stage loaded")
            return False
        try:
            usda = self._loader.build_inline_root(resolved, render_mode=render_mode_str)
            self._renderer.open_inline_root(usda)
            self._render_mode = mode
            log.info("Render mode set via inline root reload: %s → %s", mode, render_mode_str)
            return True
        except Exception as exc:
            log.warning("_apply_render_mode(%s) failed: %s", mode, exc)
            return False

    @staticmethod
    def _count_stage_prims(stage_path: str) -> int:
        """Count traversable prims in a USD stage (read-only, pxr)."""
        try:
            from pxr import Usd
            stage = Usd.Stage.Open(stage_path)
            return sum(1 for _ in stage.TraverseAll())
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # Spherical camera  (render-thread-only)
    # ------------------------------------------------------------------ #
    def _init_camera_state(self) -> None:
        """Reset spherical camera state to the default eye/target.

        Called every time a stage loads so orbit/pan/zoom start from a
        sensible position relative to the new scene.  Marks _cam_dirty so
        the render loop will push the matrix on the next iteration.
        """
        d = np.array(_DEFAULT_EYE, dtype=np.float64)
        r = float(np.linalg.norm(d))
        self._cam_r      = max(r, 1.0)
        self._cam_el     = float(math.asin(max(-1.0, min(1.0, d[1] / self._cam_r))))
        cos_el           = math.cos(self._cam_el)
        self._cam_az     = float(math.atan2(d[0], d[2])) if cos_el > 1e-9 else 0.0
        self._cam_target = (float(_DEFAULT_TARGET[0]),
                            float(_DEFAULT_TARGET[1]),
                            float(_DEFAULT_TARGET[2]))
        self._cam_dirty  = True

    def _eye_from_spherical(self) -> tuple[float, float, float]:
        """Compute world-space eye position from current spherical state."""
        cos_el = math.cos(self._cam_el)
        return (
            self._cam_target[0] + self._cam_r * cos_el * math.sin(self._cam_az),
            self._cam_target[1] + self._cam_r * math.sin(self._cam_el),
            self._cam_target[2] + self._cam_r * cos_el * math.cos(self._cam_az),
        )

    def _maybe_write_camera(self) -> None:
        """Write omni:xform if the camera state has changed since last write.

        Render-thread-only.  Called each frame after draining commands, and
        once by the renderer warmup before shader compilation.
        """
        if not self._cam_dirty:
            return
        eye = self._eye_from_spherical()
        mat = _look_at_row_major(eye, self._cam_target)
        self._renderer.write_camera_xform(np.array(mat, dtype=np.float64))
        self._cam_dirty = False

    def _apply_orbit(self, dx: float, dy: float) -> None:
        """Left-drag: rotate camera around the target point.

        dx → azimuth (yaw), dy → elevation (pitch).
        Elevation is clamped to ±89° to prevent gimbal flip at the poles.
        """
        SENS = 0.005   # radians per pixel
        self._cam_az -= dx * SENS
        self._cam_el  = max(-math.radians(89), min(math.radians(89),
                            self._cam_el + dy * SENS))
        self._cam_dirty = True

    def _apply_pan(self, dx: float, dy: float) -> None:
        """Right-drag: translate the look-at target in the camera's right/up plane.

        Pan speed scales with radius so distant objects feel the same as near ones.
        """
        eye      = np.array(self._eye_from_spherical(), dtype=np.float64)
        tgt      = np.array(self._cam_target,           dtype=np.float64)
        world_up = np.array([0.0, 1.0, 0.0],            dtype=np.float64)

        fwd = tgt - eye
        fwd_len = np.linalg.norm(fwd)
        if fwd_len < 1e-9:
            return
        fwd /= fwd_len

        right = np.cross(fwd, world_up)
        rlen  = np.linalg.norm(right)
        right = right / rlen if rlen > 1e-9 else np.array([1.0, 0.0, 0.0])

        up_cam = np.cross(right, fwd)   # camera up (orthogonal to fwd and right)

        pan_scale = self._cam_r * 0.001
        delta = (-right * dx + up_cam * dy) * pan_scale
        t = tgt + delta
        self._cam_target = (float(t[0]), float(t[1]), float(t[2]))
        self._cam_dirty  = True

    def _apply_zoom(self, delta: float) -> None:
        """Scroll wheel: scale the orbital radius.

        delta > 0 → scroll down → zoom out (larger radius).
        Clamped to 1 cm minimum so the camera never passes through the target.
        """
        self._cam_r     = max(1.0, self._cam_r * (1.0 + delta * 0.001))
        self._cam_dirty = True

    # ------------------------------------------------------------------ #
    # ovstream restart (background thread — fires after every disconnect)
    # ------------------------------------------------------------------ #
    def _restart_stream(self) -> None:
        """Stop and recreate the ovstream server to get a clean NVST state.

        The ovstream SDK's internal NVST handle (server[N]) becomes invalid
        after the first client disconnects.  Subsequent connections hit
        NVST_R_INVALID_STATE → H264 encoder never initializes
        ("Unsupported Video Codec 0") → all frames render black.

        Restarting ovstream (but NOT ovrtx) avoids the 90-second shader
        warmup while giving the next client a completely clean NVST context.
        """
        if self._stop.is_set():
            return                      # server is shutting down — skip restart

        log.info("Restarting ovstream after disconnect …")

        # Let the SDK finish its own teardown before we destroy the server.
        time.sleep(1.5)

        old_stream = self._stream
        try:
            old_stream.stop()           # server.stop() + server.close() + shutdown()
        except Exception:
            log.warning("ovstream stop() raised (ignored)", exc_info=True)

        time.sleep(1.0)                 # brief pause before re-init

        if self._stop.is_set():
            return

        # Rebuild a brand-new StreamServer (fresh ovstream.initialize() call).
        new_stream = StreamServer(self.cfg)
        new_stream.on_connection_cb = self._on_connection
        new_stream.on_message_cb    = self._on_message
        new_stream.on_input_cb      = self._on_input

        # Swap atomically (GIL makes a single attribute write atomic in CPython).
        # The render loop's stream_video() calls on the old (stopped) server
        # are no-ops (self._server is None check in StreamServer.stream_video).
        self._stream = new_stream

        try:
            new_stream.start()
            log.info("ovstream restarted — ready for next client")
        except Exception:
            log.exception("ovstream restart failed — server may need manual restart")

    # ------------------------------------------------------------------ #
    # ovstream callbacks  (called from StreamSDK internal threads)
    # Keep fast — enqueue; never call renderer APIs directly.
    # ------------------------------------------------------------------ #
    def _on_connection(self, connected: bool) -> None:
        log.info("Client %s", "connected" if connected else "disconnected")
        if connected:
            # Push current server state to the newly connected browser
            self._stream.send_event(
                Events.LOADING_STATE_RESPONSE,
                {"state": self._state, "stage": self._current_stage},
            )
            if self._current_stage and self._state == self.STREAMING:
                self._stream.send_event(
                    Events.OPEN_STAGE_RESULT,
                    {"result": "ok", "url": self._current_stage},
                )
        else:
            # Restart ovstream on a background thread so the next connection
            # gets a clean NVST state (avoids NVST_R_INVALID_STATE + codec 0).
            t = threading.Thread(
                target=self._restart_stream, name="ovstream-restart", daemon=True
            )
            t.start()

    def _on_message(self, msg: str) -> None:
        """JSON data-channel message from browser — enqueue for the render thread."""
        try:
            data = json.loads(msg)
            self._cmd_q.put(data)
        except json.JSONDecodeError:
            log.warning("Non-JSON message (first 200 chars): %s", msg[:200])

    def _on_input(self, event) -> None:
        """NVST input event — process mouse drags for orbit/pan server-side.

        Called on the StreamSDK internal thread; must NOT call renderer APIs
        directly.  Enqueues camera commands via _cmd_q for the render thread.

        Why server-side?  AppStreamer (ovstream) intercepts all browser mouse
        events before our JS handlers can see them, so frontend drag tracking
        is unreliable.  The NVST input channel already delivers every mouse
        event here — we do the drag bookkeeping ourselves.

        NVST MOVE events always report button_state=UP even during a drag;
        button hold state comes from separate BUTTON DOWN/UP events.
        X11 button convention: data=1 = left button, data=3 = right button.
        """
        try:
            mouse = event.mouse
            if mouse is None:
                return

            ev_type   = mouse.type.value          # 0 = MOVE, 2 = BUTTON
            btn_state = mouse.button_state.value  # 0 = UP,   1 = DOWN

            if ev_type == 2:  # BUTTON press or release
                if btn_state == 1:  # DOWN — start tracking
                    self._mouse_btn     = mouse.data  # 1=left, 3=right
                    self._mouse_x       = mouse.x
                    self._mouse_y       = mouse.y
                    self._click_start_x = mouse.x
                    self._click_start_y = mouse.y
                    log.debug("Mouse btn %d DOWN at (%d, %d)",
                              mouse.data, mouse.x, mouse.y)
                else:  # UP — stop tracking; check for click
                    if self._mouse_btn == mouse.data:
                        dx = abs(mouse.x - self._click_start_x)
                        dy = abs(mouse.y - self._click_start_y)
                        if dx < self._CLICK_THRESHOLD and dy < self._CLICK_THRESHOLD:
                            # Left-click with minimal drag → pick query
                            if mouse.data == 1:
                                self._cmd_q.put({
                                    "event_type": Events.PICK_REQUEST,
                                    "payload":    {"x": self._click_start_x,
                                                   "y": self._click_start_y},
                                })
                        self._mouse_btn = 0
                    log.debug("Mouse btn %d UP", mouse.data)

            elif ev_type == 0:  # MOVE
                dx = mouse.x - self._mouse_x
                dy = mouse.y - self._mouse_y
                self._mouse_x = mouse.x
                self._mouse_y = mouse.y

                if self._mouse_btn == 0 or (dx == 0 and dy == 0):
                    return  # no button held or no movement

                if self._mouse_btn == 1:    # left → orbit
                    self._cmd_q.put({
                        "event_type": Events.CAMERA_ORBIT,
                        "payload":    {"dx": float(dx), "dy": float(dy)},
                    })
                elif self._mouse_btn == 3:  # right → pan
                    self._cmd_q.put({
                        "event_type": Events.CAMERA_PAN,
                        "payload":    {"dx": float(dx), "dy": float(dy)},
                    })

        except Exception:
            log.debug("_on_input error", exc_info=True)
