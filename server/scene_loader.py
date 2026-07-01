# SPDX-License-Identifier: Apache-2.0
"""Scene loader — builds the inline USDA root and resolves stage paths.

The inline root:
  - sublayers the user USD via subLayers (never modifies the user file)
  - authors a viewer-owned /OVCamera
  - authors /Render/OVServer/ViewportTexture0  (RenderProduct)
  - authors /Render/OVServer/LdrColor          (RenderVar)
  - authors /Render/Settings

All viewer state lives in the inline root, never in the user file.

All methods are render-thread-only.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from .config import RENDER_PRODUCT_PATH, VIEWER_CAMERA_PATH, ServerConfig

log = logging.getLogger("viewer.scene_loader")

# Default camera position — isometric-ish view at 2000 cm (20 m) from origin.
# Omniverse scenes default to centimeters.  2000 cm gives a wide enough view
# to see small assets (like warp's 50-100 cm sphere) without being inside them.
_DEFAULT_EYE    = (2000.0, 2000.0, 2000.0)
_DEFAULT_TARGET = (0.0, 0.0, 0.0)
_DEFAULT_UP     = (0.0, 1.0, 0.0)


def _look_at_row_major(
    eye: tuple, target: tuple, up: tuple = _DEFAULT_UP
) -> list[float]:
    """Compute a row-major 4×4 camera matrix for omni:xform.

    Convention (from conventions.md):
      row0 = right, row1 = up_ortho, row2 = -forward, row3 = eye
    All rows in world space, last element = 0 (rows 0-2) / 1 (row 3).
    """
    eye    = np.array(eye,    dtype=np.float64)
    target = np.array(target, dtype=np.float64)
    up     = np.array(up,     dtype=np.float64)

    fwd = target - eye
    fwd_len = np.linalg.norm(fwd)
    fwd = fwd / fwd_len if fwd_len > 1e-9 else np.array([0.0, 0.0, -1.0])

    right = np.cross(fwd, up)
    right_len = np.linalg.norm(right)
    right = right / right_len if right_len > 1e-9 else np.array([1.0, 0.0, 0.0])

    up_ortho = np.cross(right, fwd)

    return [
        *right.tolist(),    0.0,
        *up_ortho.tolist(), 0.0,
        *(-fwd).tolist(),   0.0,
        *eye.tolist(),      1.0,
    ]


def build_inline_root_usda(stage_path: str, cfg: ServerConfig, render_mode: str = "RaytracedLighting") -> str:
    """Return an inline USDA string that sublayers *stage_path* and injects
    the viewer render config (camera, render product, render var, settings).

    The user stage is never modified — it is referenced via subLayers only.
    """
    # USDA paths use forward slashes on all platforms
    escaped = stage_path.replace("\\", "/")

    w, h    = cfg.width, cfg.height
    aspect  = w / h
    focal   = 35.0          # mm
    hapert  = focal * aspect

    # Embed the default lookat as a USD xformOp so the camera is positioned
    # correctly even before write_attribute("omni:xform") is called.
    # Row-major: row0=right, row1=up_ortho, row2=-forward, row3=eye.
    m = _look_at_row_major(_DEFAULT_EYE, _DEFAULT_TARGET)
    cam_xform = (
        f"(({m[0]:.6f}, {m[1]:.6f}, {m[2]:.6f}, {m[3]:.6f}), "
        f"({m[4]:.6f}, {m[5]:.6f}, {m[6]:.6f}, {m[7]:.6f}), "
        f"({m[8]:.6f}, {m[9]:.6f}, {m[10]:.6f}, {m[11]:.6f}), "
        f"({m[12]:.6f}, {m[13]:.6f}, {m[14]:.6f}, {m[15]:.6f}))"
    )

    return f"""\
#usda 1.0
(
    defaultPrim = "World"
    subLayers = [
        @{escaped}@
    ]
)

# ── Viewer-owned camera (never modifies the user stage) ───────────────────────
def Camera "{VIEWER_CAMERA_PATH.lstrip('/')}"
{{
    matrix4d xformOp:transform = {cam_xform}
    uniform token[] xformOpOrder = ["xformOp:transform"]
    float focalLength        = {focal:.4f}
    float horizontalAperture = {hapert:.4f}
    float verticalAperture   = {focal:.4f}
    float2 clippingRange     = (0.01, 100000)
}}

# ── Fallback ambient light — ensures scene is visible when the user stage has
#    no lights of its own.  A full-intensity white DomeLight with intensity 1000
#    will illuminate any geometry.  visibleInPrimaryRay=0 keeps the background
#    black (no distracting white sky dome).
def DomeLight "ViewerDomeLight"
{{
    float inputs:intensity = 1000.0
    bool inputs:visibleInPrimaryRay = 0
}}

# ── Render wiring ─────────────────────────────────────────────────────────────
def Scope "Render"
{{
    def Scope "OVServer"
    {{
        def RenderProduct "ViewportTexture0"
        {{
            rel orderedVars = [<{RENDER_PRODUCT_PATH.replace("ViewportTexture0","LdrColor")}>]
            rel camera = <{VIEWER_CAMERA_PATH}>
            int2 resolution = ({w}, {h})
        }}
        def RenderVar "LdrColor"
        {{
            token dataType = "color4f"
            custom string sourceName = "LdrColor"
            token sourceType = "raw"
        }}
    }}
    def RenderSettings "Settings"
    {{
        rel products = [<{RENDER_PRODUCT_PATH}>]
        token aspectRatioConformPolicy = "expandAperture"
        bool instantaneousShutter = 1
        token renderMode = "{render_mode}"
    }}
}}
"""


class SceneLoader:
    """Manages stage path resolution, inline root construction, and session authoring.

    All public methods must be called from the render thread only.

    Session layer tracks prims created/deactivated by the user and injects them
    into the inline USDA root (never the user file).  Undo/redo stacks allow
    reverting up to 50 operations.
    """

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.current_stage_path: str | None = None
        self._default_cam_xform = _look_at_row_major(_DEFAULT_EYE, _DEFAULT_TARGET)

        # ── Session layer (authored prims / overrides) ──────────────────────────────
        self._current_resolved: str | None = None   # last resolved path
        self._authored_prims: list[dict] = []       # [{type, name, usda}]
        self._deactivated: set[str] = set()         # prim paths with active=false
        self._undo_stack: list[tuple] = []          # snapshots (authored, deactivated)
        self._redo_stack: list[tuple] = []

    # ------------------------------------------------------------------ #
    # Session layer management
    # ------------------------------------------------------------------ #

    def reset_session(self) -> None:
        """Clear session state. Call when loading a new user stage."""
        self._authored_prims = []
        self._deactivated    = set()
        self._undo_stack     = []
        self._redo_stack     = []
        log.info("Session layer reset")

    def _snapshot(self) -> tuple:
        # Deep-copy each prim dict so mutable fields (translate list) are independent
        return (
            [{**p, "translate": list(p["translate"])} for p in self._authored_prims],
            set(self._deactivated),
        )

    def _push_undo(self) -> None:
        self._undo_stack.append(self._snapshot())
        self._redo_stack.clear()
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def create_prim(self, prim_type: str, name: str) -> "str | None":
        """Author a new prim into the session layer and return the updated USDA."""
        if not self._current_resolved:
            log.warning("create_prim: no stage loaded")
            return None
        safe = re.sub(r"[^A-Za-z0-9_]", "_", name) or "Prim"
        if not safe[0].isalpha() and safe[0] != "_":
            safe = "P" + safe
        if self._make_prim_usda(prim_type, safe) is None:
            log.warning("create_prim: unknown type %r", prim_type)
            return None
        self._push_undo()
        self._authored_prims.append({
            "type":      prim_type,
            "name":      safe,
            "translate": [0.0, 0.0, 0.0],
        })
        log.info("Session: created %s %r", prim_type, safe)
        return self.build_inline_root(self._current_resolved)

    def deactivate_prim(self, path: str) -> "str | None":
        """Author active=false override for *path* and return the updated USDA."""
        if not self._current_resolved or not path:
            return None
        self._push_undo()
        self._deactivated.add(path)
        log.info("Session: deactivated %s", path)
        return self.build_inline_root(self._current_resolved)

    def undo(self) -> "str | None":
        """Pop the undo stack and return the reverted inline USDA, or None."""
        if not self._undo_stack or not self._current_resolved:
            return None
        self._redo_stack.append(self._snapshot())
        self._authored_prims, self._deactivated = self._undo_stack.pop()
        log.info("Session: undo  (undo_stack=%d)", len(self._undo_stack))
        return self.build_inline_root(self._current_resolved)

    def redo(self) -> "str | None":
        """Pop the redo stack and return the re-applied inline USDA, or None."""
        if not self._redo_stack or not self._current_resolved:
            return None
        self._undo_stack.append(self._snapshot())
        self._authored_prims, self._deactivated = self._redo_stack.pop()
        log.info("Session: redo  (redo_stack=%d)", len(self._redo_stack))
        return self.build_inline_root(self._current_resolved)

    def _build_session_content(self) -> str:
        """Produce the session layer block to append to the inline USDA."""
        parts: list[str] = []
        for path in sorted(self._deactivated):
            usda = self._make_deactivate_usda(path)
            if usda:
                parts.append(usda)
        for prim in self._authored_prims:
            usda = self._make_prim_usda(prim["type"], prim["name"], prim["translate"])
            if usda:
                parts.append(usda)
        return "\n\n".join(parts)

    def get_session_prim_info(self, path: str) -> "dict | None":
        """Return metadata for a session-authored prim (top-level only), or None."""
        parts = path.strip("/").split("/")
        if len(parts) != 1:
            return None
        name = parts[0]
        for p in self._authored_prims:
            if p["name"] == name:
                return p.copy()
        return None

    def set_session_prim_xform(self, path: str, translate: list) -> "str | None":
        """Update the translate of a session prim and return updated USDA."""
        parts = path.strip("/").split("/")
        if len(parts) != 1:
            return None
        name = parts[0]
        for p in self._authored_prims:
            if p["name"] == name:
                self._push_undo()
                p["translate"] = [float(v) for v in translate]
                log.info("Session: moved %s → %s", name, translate)
                return self.build_inline_root(self._current_resolved)
        return None

    @staticmethod
    def _make_deactivate_usda(prim_path: str) -> str:
        """Generate nested ``over`` blocks that set *active = false* on *prim_path*."""
        parts = [p for p in prim_path.strip("/").split("/") if p]
        if not parts:
            return ""
        lines: list[str] = []
        for i, part in enumerate(parts):
            indent  = "    " * i
            is_last = (i == len(parts) - 1)
            if is_last:
                lines.append(f'{indent}over "{part}" (')
                lines.append(f"{indent}    active = false")
                lines.append(f"{indent})")
                lines.append(f"{indent}{{")
                lines.append(f"{indent}}}")
            else:
                lines.append(f'{indent}over "{part}"')
                lines.append(f"{indent}{{")
        # Close the non-terminal over blocks in reverse order
        for i in range(len(parts) - 2, -1, -1):
            lines.append("    " * i + "}")
        return "\n".join(lines)

    @staticmethod
    def _make_prim_usda(prim_type: str, name: str, translate: "list | None" = None) -> "str | None":
        n  = name
        tx, ty, tz = (translate or [0.0, 0.0, 0.0])
        t  = f"({tx}, {ty}, {tz})"
        if prim_type == "Sphere":
            return (
                f'def Sphere "{n}"\n'
                "{\n"
                "    double radius = 50.0\n"
                f"    float3 xformOp:translate = {t}\n"
                '    uniform token[] xformOpOrder = ["xformOp:translate"]\n'
                "}"
            )
        if prim_type == "Cube":
            return (
                f'def Cube "{n}"\n'
                "{\n"
                "    double size = 100.0\n"
                f"    float3 xformOp:translate = {t}\n"
                '    uniform token[] xformOpOrder = ["xformOp:translate"]\n'
                "}"
            )
        if prim_type == "Cylinder":
            return (
                f'def Cylinder "{n}"\n'
                "{\n"
                "    double height = 100.0\n"
                "    double radius = 50.0\n"
                f"    float3 xformOp:translate = {t}\n"
                '    uniform token[] xformOpOrder = ["xformOp:translate"]\n'
                "}"
            )
        if prim_type == "Xform":
            return (
                f'def Xform "{n}"\n'
                "{\n"
                f"    float3 xformOp:translate = {t}\n"
                '    uniform token[] xformOpOrder = ["xformOp:translate"]\n'
                "}"
            )
        if prim_type == "DomeLight":
            return (
                f'def DomeLight "{n}"\n'
                "{\n"
                "    float inputs:intensity = 1000.0\n"
                "}"
            )
        return None

    # ------------------------------------------------------------------ #
    # Telemetry helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def discover_xformable_prims(resolved_path: str) -> list[dict]:
        """Return Xformable prims from *resolved_path*, ranked for telemetry binding.

        Excludes cameras, lights, and the viewer's own OVCamera/Render prims.
        Returns list of {path, name, type} dicts ordered: Mesh first, then Xform.
        """
        try:
            from pxr import Usd, UsdGeom
        except ImportError:
            log.warning("discover_xformable_prims: pxr not available")
            return []

        EXCLUDE_TYPES = {"Camera", "DomeLight", "DistantLight", "SphereLight",
                         "DiskLight", "CylinderLight", "RectLight"}
        RANK = {"Mesh": 0, "Xform": 1}

        try:
            stage = Usd.Stage.Open(resolved_path)
        except Exception as exc:
            log.warning("discover_xformable_prims: cannot open %s: %s", resolved_path, exc)
            return []

        results = []
        for prim in stage.Traverse():
            path = str(prim.GetPath())
            if path.startswith("/OVCamera") or path.startswith("/Render"):
                continue
            prim_type = prim.GetTypeName()
            if prim_type in EXCLUDE_TYPES:
                continue
            if not UsdGeom.Xformable(prim):
                continue
            results.append({
                "path": path,
                "name": prim.GetName(),
                "type": prim_type or "Xformable",
                "_rank": RANK.get(prim_type, 2),
            })

        results.sort(key=lambda p: (p["_rank"], p["path"]))
        for r in results:
            del r["_rank"]
        return results

    def generate_telemetry_usda(
        self,
        resolved_path: str,
        bindings: list[dict],
        duration: float = 30.0,
        fps: float = 24.0,
    ) -> str:
        """Build a complete inline-root USDA with timeSamples that animate bound prims.

        Starts from the standard inline root (camera + render wiring + sublayer of
        resolved_path), injects timecode metadata, then appends timeSampled overrides.
        The result is safe to pass directly to open_inline_root() — the render product
        is always present because we use build_inline_root_usda() as the base.

        Each binding dict: {path, channel_type, amplitude, frequency}

        channel_type options:
            oscillate_x / oscillate_y / oscillate_z — sine-wave translate on that axis
            rotate_z    — sine-wave Z rotation in degrees
            alert_pulse — active bool pulses on/off at given frequency
            linear      — conveyor: straight-line on XZ plane; frequency field = angle °
        """
        import math

        total_frames = int(duration * fps)
        end_tc       = total_frames - 1

        # Pre-read base translate AND rotateXYZ for every bound prim so we can
        # offset all motion from the prim's natural position/orientation.
        # Without this, oscillate would zero-out the other axes and teleport
        # the prim to the origin; rotate_z would snap X/Y rotation to 0.
        prim_base_translate: dict[str, tuple] = {}   # (x, y, z) cm
        prim_base_rotate:    dict[str, tuple] = {}   # (rx, ry, rz) degrees

        all_paths = list({b.get("path", "") for b in bindings if b.get("path")})
        if all_paths:
            try:
                from pxr import Usd, UsdGeom  # type: ignore
                _stage = Usd.Stage.Open(resolved_path)
                for _path in all_paths:
                    _prim = _stage.GetPrimAtPath(_path)
                    if not (_prim and _prim.IsValid()):
                        continue
                    # Translate — extract from local transformation matrix
                    _xf = UsdGeom.Xformable(_prim)
                    if _xf:
                        _mat, _ = _xf.GetLocalTransformation()
                        _t = _mat.ExtractTranslation()
                        prim_base_translate[_path] = (float(_t[0]), float(_t[1]), float(_t[2]))
                    # RotateXYZ — read directly from the attribute (most Omniverse/Kit
                    # prims use xformOp:rotateXYZ).  Fall back to (0,0,0) if absent.
                    _rot_attr = _prim.GetAttribute("xformOp:rotateXYZ")
                    if _rot_attr and _rot_attr.IsValid():
                        _rv = _rot_attr.Get()
                        if _rv is not None:
                            prim_base_rotate[_path] = (float(_rv[0]), float(_rv[1]), float(_rv[2]))
                        else:
                            prim_base_rotate[_path] = (0.0, 0.0, 0.0)
                    else:
                        prim_base_rotate[_path] = (0.0, 0.0, 0.0)
            except Exception as _exc:
                log.warning("generate_telemetry_usda: could not read prim transforms: %s", _exc)

        # Group bindings by prim path — one prim may have multiple channels.
        # IMPORTANT: we do NOT override xformOpOrder in any attr_block.
        # The base USD layer already declares it; our `over` inherits it via
        # sublayer composition.  Overriding it here would destroy the prim's
        # scale and rotation that come from the other ops in the order.
        prim_blocks: dict[str, list[str]] = {}
        for b in bindings:
            path      = b.get("path", "")
            ch        = b.get("channel_type", "oscillate_x")
            amplitude = float(b.get("amplitude", 50.0))
            frequency = float(b.get("frequency", 0.5))
            if not path:
                continue

            bx, by, bz   = prim_base_translate.get(path, (0.0, 0.0, 0.0))
            brx, bry, brz = prim_base_rotate.get(path, (0.0, 0.0, 0.0))

            samples: list[str] = []
            for frame in range(total_frames):
                t   = frame / fps
                val = amplitude * math.sin(2 * math.pi * frequency * t)

                if ch in ("oscillate_x", "oscillate_y", "oscillate_z"):
                    # Full Vec3 translate — base + delta on the target axis only.
                    # Other axes stay at their original position (no origin snap).
                    if ch == "oscillate_x":
                        v = f"({bx + val:.4f}, {by:.4f}, {bz:.4f})"
                    elif ch == "oscillate_y":
                        v = f"({bx:.4f}, {by + val:.4f}, {bz:.4f})"
                    else:
                        v = f"({bx:.4f}, {by:.4f}, {bz + val:.4f})"
                    samples.append(f"                {frame}: {v},")
                elif ch == "rotate_z":
                    # Full Vec3 rotateXYZ — base rotation + delta on Z only.
                    # Preserves any original X/Y tilt from the authored scene.
                    v = f"({brx:.4f}, {bry:.4f}, {brz + val:.4f})"
                    samples.append(f"                {frame}: {v},")
                elif ch == "alert_pulse":
                    # Visibility blink — works on every prim regardless of material.
                    period_frames = max(1, int(fps / max(frequency, 0.01)))
                    is_on = (frame % period_frames) < (period_frames // 2)
                    vis = '"inherited"' if is_on else '"invisible"'
                    samples.append(f"                {frame}: {vis},")
                elif ch == "linear":
                    # Conveyor: ping-pong triangle wave on XZ plane at angle degrees.
                    # frequency field reused as angle (0°=+X, 90°=+Z).
                    angle_rad = math.radians(frequency)
                    t_norm = frame / total_frames                           # 0 → 1
                    pos    = amplitude * (1.0 - abs(2.0 * t_norm - 1.0))  # triangle: 0→amp→0
                    lx     = bx + math.cos(angle_rad) * pos
                    lz     = bz + math.sin(angle_rad) * pos
                    samples.append(f"                {frame}: ({lx:.4f}, {by:.4f}, {lz:.4f}),")
                else:
                    continue

            sample_block = "\n".join(samples)

            if ch in ("oscillate_x", "oscillate_y", "oscillate_z", "linear"):
                attr_block = (
                    "            float3 xformOp:translate.timeSamples = {\n"
                    f"{sample_block}\n"
                    "            }"
                )
            elif ch == "rotate_z":
                attr_block = (
                    "            float3 xformOp:rotateXYZ.timeSamples = {\n"
                    f"{sample_block}\n"
                    "            }"
                )
            elif ch == "alert_pulse":
                attr_block = (
                    "            token visibility.timeSamples = {\n"
                    f"{sample_block}\n"
                    "            }"
                )
            else:
                continue

            prim_blocks.setdefault(path, []).append(attr_block)

        # Build a path tree so all prims that share a common ancestor (e.g. /World)
        # end up inside ONE `over "World" { }` block.  Generating a separate
        # `over "World"` per prim produces duplicate top-level specs, which USD
        # rejects with "Duplicate prim 'World'".
        def _make_node() -> dict:
            return {"attrs": [], "children": {}}

        tree = _make_node()
        for path, attr_list in prim_blocks.items():
            parts = [p for p in path.strip("/").split("/") if p]
            node  = tree
            for part in parts:
                node["children"].setdefault(part, _make_node())
                node = node["children"][part]
            node["attrs"].extend(attr_list)

        def _render_tree(node: dict, depth: int) -> list[str]:
            lines = []
            for name, child in node["children"].items():
                pad = "    " * depth
                lines.append(f'{pad}over "{name}"')
                lines.append(f'{pad}{{')
                for al in child["attrs"]:
                    lines.append(al)
                lines.extend(_render_tree(child, depth + 1))
                lines.append(f'{pad}}}')
            return lines

        overrides_usda = "\n".join(_render_tree(tree, 0))

        # Build the COMPLETE inline root (camera + render wiring + sublayer)
        # using the same builder as normal scene loads, so the render product
        # is always present in the USDA passed to open_inline_root().
        base = build_inline_root_usda(resolved_path, self.cfg)

        # Inject timecode metadata into the USDA header, right after defaultPrim.
        timecode_lines = (
            f"    startTimeCode = 0\n"
            f"    endTimeCode = {end_tc}\n"
            f"    timeCodesPerSecond = {fps:.1f}\n"
        )
        base = base.replace(
            '    defaultPrim = "World"\n',
            f'    defaultPrim = "World"\n{timecode_lines}',
        )

        return (
            base.rstrip()
            + "\n\n# ── Telemetry — auto-generated timeSample overrides ─────────────────────\n"
            + overrides_usda
            + "\n"
        )

    # ------------------------------------------------------------------ #
    # Path resolution
    # ------------------------------------------------------------------ #
    def resolve(self, requested: str) -> str | None:
        """Resolve *requested* to an absolute path or passthrough URL.

        Returns None if the path cannot be found.
        """
        # Remote URLs — pass through as-is
        if requested.startswith(("omniverse://", "http://", "https://", "s3://")):
            return requested

        p = Path(requested)
        if p.is_absolute():
            return str(p) if p.exists() else None

        # Relative — try asset root
        candidate = self.cfg.asset_root / requested
        if candidate.exists():
            return str(candidate)

        log.warning(
            "Stage not found: %r  (asset_root=%s)", requested, self.cfg.asset_root
        )
        return None

    # ------------------------------------------------------------------ #
    # Inline root builder
    # ------------------------------------------------------------------ #
    def build_inline_root(self, resolved_path: str, render_mode: str = "RaytracedLighting") -> str:
        """Build the full inline USDA: base config + session layer."""
        self._current_resolved = resolved_path   # remember for session ops
        base    = build_inline_root_usda(resolved_path, self.cfg, render_mode=render_mode)
        session = self._build_session_content()
        if session:
            base = (base.rstrip()
                    + "\n\n# ── Session layer (authored prims / overrides) ───────────────────────────\n"
                    + session + "\n")
        return base

    # ------------------------------------------------------------------ #
    # Default camera
    # ------------------------------------------------------------------ #
    def default_camera_xform(self) -> list[float]:
        """16-float row-major identity-ish camera xform for the default view."""
        return self._default_cam_xform
