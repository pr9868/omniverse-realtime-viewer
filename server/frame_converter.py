# SPDX-License-Identifier: Apache-2.0
"""CUDA frame conversion — RGBA8 (ovrtx) → BGRA8 (ovstream).

Uses a Warp kernel for in-place R/B channel swap on the GPU.
No CPU round trip in the normal streaming path.

Usage:
    converter = FrameConverter(width, height)
    converter.ensure_buffer()  # call once after warp.init()

    # Inside render loop — inside ldr_var.map() context:
    bgra = converter.rgba_to_bgra(mapped_dlpack_tensor)
    stream_server.stream_video(bgra)
"""
from __future__ import annotations

import logging

import warp as wp

log = logging.getLogger("viewer.frame_converter")


# ── Warp kernel: swap R and B channels in-place (RGBA → BGRA) ─────────────────
@wp.kernel
def _swap_rb(img: wp.array3d(dtype=wp.uint8)):
    i, j, k = wp.tid()
    if k == 0 or k == 2:
        r = img[i, j, 0]
        b = img[i, j, 2]
        img[i, j, 0] = b
        img[i, j, 2] = r


class FrameConverter:
    """Converts ovrtx LdrColor (RGBA8, CUDA) → ovstream VideoFrame (BGRA8, CUDA).

    The persistent BGRA buffer is allocated once and reused every frame to avoid
    per-frame GPU allocations.  Keep the buffer alive until stream_video() returns.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width  = width
        self.height = height
        self._bgra: wp.array | None = None  # persistent CUDA BGRA8 buffer

    def ensure_buffer(self) -> None:
        """Allocate the persistent BGRA stream buffer.  Call after warp.init()."""
        if self._bgra is None:
            self._bgra = wp.empty(
                (self.height, self.width, 4),
                dtype=wp.uint8,
                device="cuda",
            )
            log.info("BGRA stream buffer allocated: %dx%d", self.width, self.height)

    def rgba_to_bgra(self, dlpack_tensor) -> wp.array:
        """Copy a DLPack RGBA8 tensor to the persistent CUDA buffer and swap R/B.

        Args:
            dlpack_tensor: Object from inside ``ldr_var.map()`` that supports
                           __dlpack__ (ovrtx MappedRenderVar).

        Returns:
            The internal wp.array BGRA8 buffer on the CUDA device.
            Keep the return value alive until stream_video() returns.
        """
        assert self._bgra is not None, "Call ensure_buffer() before rgba_to_bgra()"

        # Zero-copy import from CUDA (DLPack protocol)
        src = wp.from_dlpack(dlpack_tensor)

        # Copy source into persistent buffer, then swap R/B in-place
        wp.copy(self._bgra, src)
        wp.launch(
            _swap_rb,
            dim=(self.height, self.width, 4),
            inputs=[self._bgra],
        )
        wp.synchronize()

        return self._bgra

    @property
    def bgra_buffer(self) -> wp.array | None:
        """Direct access to the persistent BGRA buffer (for diagnostics)."""
        return self._bgra
