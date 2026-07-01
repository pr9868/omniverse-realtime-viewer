# SPDX-License-Identifier: Apache-2.0
"""Owns the ovrtx renderer. ALL methods here are render-thread-only.

Contracts (from streaming-viewer-recipe/server-runtime.md + conventions.md):
- Set OVRTX_SKIP_USD_CHECK=1 *before* importing ovrtx (done in the entry point).
- One thread owns renderer.step(), open_usd*, reset_stage, picking, and
  write_attribute(). Other callbacks enqueue work; they never call these.
- Synchronous rendering first. The app calls step() explicitly each frame.
- Pass the exact RenderProduct path to every step() call.
- Extract LdrColor (RGBA8) from the returned frame.
- Live camera updates write `omni:xform` (NOT xformOp:transform) via write_attribute.

NOTE: ovrtx's exact Python API (class/method names, kwargs) must be confirmed
against the installed package and the supplemental repo:
https://github.com/nvidia-omniverse/ovrtx
Calls that need that confirmation are marked with `# API:`.
"""
from __future__ import annotations

import logging

import numpy as np

from .config import RENDER_PRODUCT_PATH, VIEWER_CAMERA_PATH, ServerConfig

log = logging.getLogger("viewer.renderer")


class RendererRuntime:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.render_product_path = RENDER_PRODUCT_PATH
        self.camera_path = VIEWER_CAMERA_PATH
        self.frame_index = 0
        self.has_stage = False
        self._renderer = None  # ovrtx.Renderer

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def construct(self) -> None:
        """Build the ovrtx renderer once. Must run after env vars are set."""
        import ovrtx  # imported lazily, after OVRTX_SKIP_USD_CHECK=1

        # API: confirm RendererConfig field names against installed ovrtx.
        config = ovrtx.RendererConfig(
            sync_mode=True,                  # synchronous: app drives step()
            selection_outline_enabled=True,  # native selection outline for GPU picking
        )
        self._renderer = ovrtx.Renderer(config)
        log.info("ovrtx.Renderer constructed (%dx%d)", self.cfg.width, self.cfg.height)

    # ------------------------------------------------------------------ #
    # Stage load (delegated body lives in scene_loader; this does the ovrtx call)
    # ------------------------------------------------------------------ #
    def open_inline_root(self, inline_root_usda: str) -> None:
        """Load a stage from an inline USDA root string.

        Preferred over open_usd(path) because it sublayers the user stage and
        authors viewer render config without temp-file lifetime issues, while
        preserving relative asset resolution through the sublayer path.
        """
        assert self._renderer is not None
        # API: open_usd_from_string signature.
        self._renderer.open_usd_from_string(inline_root_usda)
        self.has_stage = True
        self.frame_index = 0

    def reset_stage(self) -> None:
        assert self._renderer is not None
        self._renderer.reset_stage()  # API
        self.has_stage = False

    # ------------------------------------------------------------------ #
    # Per-frame stepping
    # ------------------------------------------------------------------ #
    def step(self, delta_time: float) -> np.ndarray:
        """Render one frame and return LdrColor as a (H, W, 4) uint8 numpy array.

        Confirmed ovrtx 0.3.0 API:
          step() -> RenderProductSetOutputs  (dict-like)
          [rp_path]                          -> ProductOutput  (.frames list)
          .frames[0]                         -> FrameOutput    (.render_vars dict)
          .render_vars["LdrColor"]           -> RenderVarOutput (.map() context mgr)
          .map()  __enter__                  -> MappedRenderVar (.tensor DLPack)
        """
        assert self._renderer is not None
        self.frame_index += 1
        outputs = self._renderer.step(
            render_products={self.render_product_path},
            delta_time=delta_time,
        )
        ldr_var = outputs[self.render_product_path].frames[0].render_vars["LdrColor"]
        with ldr_var.map() as mapped:
            arr = np.from_dlpack(mapped)  # MappedRenderVar supports DLPack directly
            return arr.copy()  # copy out before unmap

    def warmup(self, write_camera_xform, n_frames: int = 8) -> None:
        """Compile shaders / allocate buffers before accepting clients.

        Cold-start shader compile is ~90s on L40/L40S (cloud-deployment ref).
        """
        write_camera_xform()
        dt = 1.0 / float(self.cfg.target_fps)
        for _ in range(n_frames):
            self.step(dt)  # discard frame; forces shader compilation
        log.info("renderer warmup complete (%d frames)", n_frames)

    # ------------------------------------------------------------------ #
    # Live attribute writes (camera, etc.) — render-thread-only
    # ------------------------------------------------------------------ #
    def write_camera_xform(self, matrix_row_major: np.ndarray) -> None:
        """Write the viewer camera transform live.

        Camera is a USD prim; we write `omni:xform` (row-major: row0=right,
        row1=up, row2=-forward, row3=eye) NOT authored xformOp:* attributes.
        """
        assert self._renderer is not None
        # write_attribute tensor shape: (n_prims, attribute_elements)
        # omni:xform is a 4x4 double matrix = 16 float64s = 128 bytes per prim.
        # Pass as (1, 16) so element_size = 128 bytes, matching Fabric's binding.
        flat = np.ascontiguousarray(matrix_row_major, dtype=np.float64).reshape(1, 16)
        self._renderer.write_attribute(
            [self.camera_path],
            "omni:xform",
            flat,
        )

    def enqueue_pick(self, x: int, y: int) -> None:
        """Enqueue a 1×1 pick query at pixel (x, y) for the next step().

        Must be called before step().  Result is read via read_pick_result()
        from the step() outputs on the same frame.
        """
        if self._renderer is None or not self.has_stage:
            return
        try:
            self._renderer.enqueue_pick_query(
                render_product_path=self.render_product_path,
                left=x,
                top=y,
                right=x + 1,
                bottom=y + 1,
            )
        except Exception:
            log.debug("enqueue_pick(%d, %d) error", x, y, exc_info=True)

    def read_pick_result(self, outputs) -> str | None:
        """Read the pick-hit render variable from step() outputs.

        Call after step() on the same frame enqueue_pick() was called.
        Returns the USD prim path string, or None if nothing was hit.
        """
        if self._renderer is None:
            return None
        try:
            import ovrtx
            import numpy as np

            frame    = outputs[self.render_product_path].frames[0]
            pick_var = frame.render_vars.get(ovrtx.OVRTX_RENDER_VAR_PICK_HIT)
            if pick_var is None:
                return None

            mapping   = pick_var.map(device=ovrtx.Device.CPU)
            magic     = int(np.from_dlpack(mapping.params["magic"]).reshape(-1)[0])
            version   = int(np.from_dlpack(mapping.params["version"]).reshape(-1)[0])
            hit_count = int(np.from_dlpack(mapping.params["hitCount"]).reshape(-1)[0])

            if magic != ovrtx.OVRTX_PICK_HIT_MAGIC or version != ovrtx.OVRTX_PICK_HIT_VERSION:
                log.warning("Unexpected pick-hit schema: magic=%d version=%d", magic, version)
                mapping.unmap()
                return None

            if hit_count == 0:
                mapping.unmap()
                return None

            path_ids = np.from_dlpack(mapping["primPath"]).copy().reshape(-1)
            mapping.unmap()

            path_id = int(path_ids[0])
            if path_id == 0:
                return None

            path = self._renderer.resolve_prim_path_id(path_id)
            return path or None

        except Exception:
            log.debug("read_pick_result() error", exc_info=True)
            return None

    @property
    def renderer(self):
        return self._renderer
