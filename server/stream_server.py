# SPDX-License-Identifier: Apache-2.0
"""ovstream WebRTC server wrapper.

Owns: ovstream lifecycle, callback registration, frame submission, JSON send.
Does NOT own: renderer stepping, frame conversion (render-thread duties).

Critical ordering rule (from streaming-server reference):
  Register callbacks BEFORE calling server.start().
  Callbacks may fire from StreamSDK internal threads — keep them fast and
  never call renderer APIs from inside them.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

import ovstream
from ovstream import LogLevel, ServerType, VideoFrame

from .config import ServerConfig

log = logging.getLogger("viewer.stream_server")


def _ovstream_log(level, channel: str, msg: str, timestamp) -> None:
    """Forward ovstream log events into Python logging."""
    py_level = {
        LogLevel.ERROR:   logging.ERROR,
        LogLevel.WARNING: logging.WARNING,
        LogLevel.INFO:    logging.INFO,
        LogLevel.VERBOSE: logging.DEBUG,
    }.get(level, logging.DEBUG)
    log.log(py_level, "[ovstream/%s] %s", channel, msg)


class StreamServer:
    """Wraps ovstream.Server for WebRTC streaming.

    Typical call order:
        stream = StreamServer(cfg)
        stream.on_connection_cb = my_handler
        stream.on_message_cb    = my_handler
        stream.on_input_cb      = my_handler
        stream.start()                          # after renderer warmup
        # ... render loop calls stream_video() and send_event()
        stream.stop()
    """

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self._server: ovstream.Server | None = None
        self._initialized = False

        # Set these before calling start()
        self.on_connection_cb: Callable | None = None
        self.on_message_cb:    Callable | None = None
        self.on_input_cb:      Callable | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Initialize ovstream and start the WebRTC server.

        Must be called AFTER renderer warmup and BEFORE the render loop.
        """
        ovstream.initialize(
            log_fn=_ovstream_log,
            log_min_severity=LogLevel.WARNING,
        )
        self._initialized = True

        version = ovstream.get_version() if hasattr(ovstream, "get_version") else "n/a"
        log.info("ovstream initialized  version=%s", version)

        self._server = ovstream.Server(ServerType.WEBRTC)

        # ── Register callbacks BEFORE start() ────────────────────────────
        if self.on_connection_cb:
            self._server.on_connection = self.on_connection_cb
        if self.on_message_cb:
            self._server.on_message = self.on_message_cb
        if self.on_input_cb:
            self._server.on_input = self.on_input_cb

        # ── Build ServerConfig ────────────────────────────────────────────
        sc = ovstream.ServerConfig(
            width=self.cfg.width,
            height=self.cfg.height,
            target_fps=self.cfg.target_fps,
            stream_port=self.cfg.media_port,
            video_input=ovstream.VideoInput.CUDA,
            webrtc_signal_port=self.cfg.signaling_port,
        )
        if self.cfg.public_ip:
            sc.webrtc_public_ip = self.cfg.public_ip

        self._server.start(sc)
        log.info(
            "WebRTC server started  signaling=%d  media=%d  public_ip=%s",
            self.cfg.signaling_port,
            self.cfg.media_port,
            self.cfg.public_ip,
        )

    def stop(self) -> None:
        """Stop the WebRTC server and shut down ovstream (one shutdown per init)."""
        if self._server is not None:
            try:
                self._server.stop()
                self._server.close()
            except Exception:
                log.debug("Error stopping ovstream server", exc_info=True)
            self._server = None
        if self._initialized:
            try:
                ovstream.shutdown()
            except Exception:
                log.debug("Error shutting down ovstream", exc_info=True)
            self._initialized = False

    # ------------------------------------------------------------------ #
    # Runtime
    # ------------------------------------------------------------------ #
    @property
    def is_client_connected(self) -> bool:
        return self._server is not None and bool(self._server.is_client_connected)

    def stream_video(self, cuda_array) -> None:
        """Submit a BGRA8 warp CUDA array as a video frame.

        Catches transient disconnect errors — render loop must not crash on them.
        """
        if self._server is None:
            return
        try:
            frame = VideoFrame.from_cuda_array(cuda_array)
            self._server.stream_video(frame)
        except Exception:
            log.debug("Dropping frame during disconnect", exc_info=True)

    def send_event(self, event_type: str, payload: dict) -> None:
        """Send a JSON data-channel message to the connected browser client.

        Silently drops the message when no client is connected.
        """
        if not self.is_client_connected:
            return
        try:
            msg = json.dumps(
                {"event_type": event_type, "payload": payload}, default=str
            )
            self._server.send_message(msg)
        except Exception:
            log.debug(
                "Dropping event during disconnect: %s", event_type, exc_info=True
            )
