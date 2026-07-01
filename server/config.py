# SPDX-License-Identifier: Apache-2.0
"""Server configuration and the single source of truth for data-channel event names.

The event name constants here MUST stay in lockstep with
``frontend/src/types/messages.ts``. Define them once, here, and import everywhere.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Data-channel event names. Mirror exactly in frontend/src/types/messages.ts.
# Event names below are the single source of truth — mirror exactly in
# frontend/src/types/messages.ts.
# --------------------------------------------------------------------------- #
class Events:
    # ---- Scene lifecycle & camera ----
    # Scene lifecycle
    OPEN_STAGE_REQUEST = "openStageRequest"
    OPEN_STAGE_RESULT = "openStageResult"
    RESET_STAGE_REQUEST = "resetStageRequest"
    LOADING_STATE_QUERY = "loadingStateQuery"
    LOADING_STATE_RESPONSE = "loadingStateResponse"
    UPDATE_PROGRESS_AMOUNT = "updateProgressAmount"
    UPDATE_PROGRESS_ACTIVITY = "updateProgressActivity"
    LIST_SCENES_REQUEST = "listScenesRequest"
    LIST_SCENES_RESULT = "listScenesResult"
    # Camera (discrete commands; continuous nav rides the native input channel)
    CAMERA_COMMAND_REQUEST = "cameraCommandRequest"
    FIT_CAMERA_REQUEST = "fitCameraRequest"
    CAMERA_STATE_CHANGED = "cameraStateChanged"
    # Camera orbit / pan / zoom (mouse navigation from browser)
    CAMERA_ORBIT = "cameraOrbit"
    CAMERA_PAN   = "cameraPan"
    CAMERA_ZOOM  = "cameraZoom"
    # ---- Pick, bookmarks, hierarchy ----
    PICK_REQUEST     = "pickRequest"      # {x, y, _resolve_event, _result_holder}
    RECALL_BOOKMARK  = "recallBookmark"   # {az, el, r, target}
    # Errors
    VIEWER_ERROR = "viewerError"

    # ---- Prim editing, search, variants, timeline, render mode ----
    EDIT_PRIM      = "editPrim"      # {path, translate?, rotate?, scale?, visibility?, _resolve_event, _result_holder}
    SAVE_STAGE     = "saveStage"     # {output_path?, _resolve_event, _result_holder}
    SET_VARIANT    = "setVariant"    # {path, variant_set, variant, _resolve_event, _result_holder}
    SET_TIMELINE   = "setTimeline"   # {time_code?, playing?, speed?, _resolve_event, _result_holder}
    SET_RENDER_MODE = "setRenderMode" # {mode, _resolve_event, _result_holder}

    # ---- Session layer authoring, undo/redo, snapshot ----
    CREATE_PRIM    = "createPrim"    # {type, name, _resolve_event, _result_holder}
    DEACTIVATE_PRIM = "deactivatePrim" # {path, _resolve_event, _result_holder}
    UNDO           = "undo"          # {_resolve_event, _result_holder}
    REDO           = "redo"          # {_resolve_event, _result_holder}

    # ---- Live telemetry simulation ----
    TELEMETRY_DISCOVER = "telemetryDiscover" # {_resolve_event, _result_holder} → {prims: [...]}
    TELEMETRY_GENERATE = "telemetryGenerate" # {bindings, duration?, fps?, _resolve_event, _result_holder}
    TELEMETRY_STOP     = "telemetryStop"     # {_resolve_event, _result_holder}

    # ---- Reserved (not yet implemented via data channel) ----
    GET_CHILDREN_REQUEST = "getChildrenRequest"
    GET_CHILDREN_RESULT = "getChildrenResult"
    GET_PROPERTIES_REQUEST = "getPropertiesRequest"
    GET_PROPERTIES_RESPONSE = "getPropertiesResponse"
    SELECT_PRIMS_REQUEST = "selectPrimsRequest"
    STAGE_SELECTION_CHANGED = "stageSelectionChanged"
    MAKE_PRIMS_SELECTABLE = "makePrimsSelectable"
    MAKE_PRIMS_PICKABLE = "makePrimsPickable"
    GET_VARIANTS_REQUEST = "getVariantsRequest"
    GET_VARIANTS_RESPONSE = "getVariantsResponse"
    SET_VARIANT_REQUEST = "setVariantRequest"
    SET_RENDER_SETTING_REQUEST = "setRenderSettingRequest"
    GET_RENDER_SETTINGS_REQUEST = "getRenderSettingsRequest"
    RENDER_SETTINGS_CHANGED = "renderSettingsChanged"


# Viewer-owned USD prim paths. These live in the inline session root that
# sublayers the (unmodified) user stage — never authored into the user file.
VIEWER_CAMERA_PATH = "/OVCamera"
RENDER_PRODUCT_PATH = "/Render/OVServer/ViewportTexture0"


@dataclass
class ServerConfig:
    # Render / stream contract (fixed for the session per conventions.md)
    width: int = 1920
    height: int = 1080
    target_fps: int = 60

    # ovstream / WebRTC
    signaling_port: int = 49100        # WebRTC signaling (WebSocket/TCP)
    media_port: int = 47998           # WebRTC media (UDP) — open this in the SG
    public_ip: str = "127.0.0.1"      # ICE candidate IP; set to your server's public IP for remote deployments
    health_port: int = 8081           # /healthz

    # Content
    initial_stage: str | None = None   # URL/path of the first scene to load
    asset_root: Path = field(default_factory=lambda: Path("assets/samples").resolve())
    settings_path: Path = field(default_factory=lambda: Path("data/viewer-settings.json").resolve())

    # Camera load policy: "fit" | "stage-camera" | "preserve"
    camera_policy: str = "fit"

    @staticmethod
    def from_args(argv: list[str] | None = None) -> "ServerConfig":
        p = argparse.ArgumentParser(description="Omniverse Realtime Viewer streaming server")
        p.add_argument("--width", type=int, default=1920)
        p.add_argument("--height", type=int, default=1080)
        p.add_argument("--fps", dest="target_fps", type=int, default=60)
        p.add_argument("--port", dest="signaling_port", type=int, default=49100)
        p.add_argument("--media-port", type=int, default=47998)
        p.add_argument("--public-ip", default="127.0.0.1")
        p.add_argument("--health-port", type=int, default=8081)
        p.add_argument("--stage", dest="initial_stage", default=None)
        p.add_argument("--asset-root", default="assets/samples")
        p.add_argument("--settings-path", default="data/viewer-settings.json")
        p.add_argument("--camera-policy", default="fit", choices=["fit", "stage-camera", "preserve"])
        a = p.parse_args(argv)
        return ServerConfig(
            width=a.width,
            height=a.height,
            target_fps=a.target_fps,
            signaling_port=a.signaling_port,
            media_port=a.media_port,
            public_ip=a.public_ip,
            health_port=a.health_port,
            initial_stage=a.initial_stage,
            asset_root=Path(a.asset_root).resolve(),
            settings_path=Path(a.settings_path).resolve(),
            camera_policy=a.camera_policy,
        )
