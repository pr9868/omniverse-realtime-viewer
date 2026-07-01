// ─── API response shapes ──────────────────────────────────────────────────────
// These mirror exactly what the Python aiohttp server returns.
// TypeScript checks every place in the app that uses these — if you rename a
// field here, VS Code immediately underlines every broken reference.

/** One USD file available on the server's asset root */
export interface Scene {
  name: string   // display name, e.g. "stage02"
  path: string   // relative path sent to /api/load, e.g. "stage02/stage02.usd"
}

/** One node in the USD prim hierarchy tree */
export interface PrimNode {
  path:         string   // full USD path, e.g. "/World/AbstractBike"
  name:         string   // just the last segment, e.g. "AbstractBike"
  type:         string   // USD prim type, e.g. "Xform", "Mesh", "Camera"
  has_children: boolean  // server tells us if this prim has children (for lazy load)
}

/** Response from /api/hierarchy — flat list of immediate children only */
export interface HierarchyResponse {
  path:     string
  children: PrimNode[]  // each node has has_children but no nested children array
}

/** Response from /api/pick */
export interface PickResponse {
  prim_path: string | null
}

/** Response from /api/status */
export interface SceneInfo {
  state:            string         // e.g. "streaming", "idle"
  scene:            string | null  // current stage path
  prim_count:       number
  cam_az_deg:       number         // azimuth in degrees
  cam_el_deg:       number         // elevation in degrees
  cam_r:            number         // distance from target in cm
  cam_target:       [number, number, number]
  last_picked_prim: string | null
}

/** One saved camera bookmark */
export interface Bookmark {
  name: string
}

/** Response from /api/bookmarks (GET) */
export interface BookmarksResponse {
  bookmarks: Bookmark[]
}

// ─── Prim properties ────────────────────────────────────────────────────────

/** One authored attribute returned by /api/prim */
export interface PrimAttr {
  name:  string
  type:  string
  value: string
}

/** Full prim properties returned by GET /api/prim?path=... */
export interface PrimProperties {
  path:       string
  type:       string
  translate:  [number, number, number]
  rotate:     [number, number, number]   // XYZ Euler degrees
  scale:      [number, number, number]
  visibility: string                     // "inherited" | "invisible"
  has_xform:  boolean
  attrs:      PrimAttr[]
  error?:     string
}

// ─── Prim extensions ────────────────────────────────────────────────────────

export interface VariantSet {
  name:    string
  choices: string[]
  current: string
}

export interface BBox {
  center:     [number, number, number]
  min:        [number, number, number]
  max:        [number, number, number]
  dimensions: [number, number, number]
  error?:     string
}

export interface MeasureResult {
  distance: number
  center_a: [number, number, number]
  center_b: [number, number, number]
  unit:     string
  error?:   string
}

export interface TimelineState {
  time_code:  number
  time_start: number
  time_end:   number
  stage_fps:  number
  is_playing: boolean
  speed:      number
  has_anim:   boolean
}

export type RenderMode = 'rtx' | 'unlit' | 'wireframe'

// ─── Session layer authoring ────────────────────────────────────────────────
export type PrimType = 'Sphere' | 'Cube' | 'Cylinder' | 'Xform' | 'DomeLight'

// ─── Telemetry simulation ───────────────────────────────────────────────────

/** One Xformable prim discovered in the current scene */
export interface TelemetryPrim {
  path: string
  name: string
  type: string
}

export type ChannelType =
  | 'oscillate_x'
  | 'oscillate_y'
  | 'oscillate_z'
  | 'rotate_z'
  | 'alert_pulse'
  | 'linear'        // conveyor: straight-line motion at a given angle on the XZ plane

/** One configured telemetry channel binding */
export interface TelemetryChannel {
  id:           string        // client-side UUID for React key
  enabled:      boolean       // if false, excluded from the next Start
  path:         string        // USD prim path
  channel_type: ChannelType
  amplitude:    number        // cm (or degrees for rotate_z)
  frequency:    number        // Hz
}

/** Payload sent to POST /api/telemetry/generate */
export interface TelemetryConfig {
  bindings: Omit<TelemetryChannel, 'id'>[]
  duration: number   // seconds
  fps:      number
}

/** State stored in the Telemetry component */
export interface TelemetryState {
  active:   boolean
  channels: TelemetryChannel[]
  duration: number
  fps:      number
}

// ─── AppStreamer global (declared in global.d.ts) ────────────────────────────
// You don't import AppStreamer — it lives on window. See src/types/global.d.ts.
