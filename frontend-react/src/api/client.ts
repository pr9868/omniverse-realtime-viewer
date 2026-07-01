/**
 * API client — matches the actual routes in server/__main__.py exactly.
 *
 * Route summary (from the server):
 *   GET  /healthz
 *   GET  /api/status                  → scene state, prim count, camera
 *   GET  /api/scenes                  → list of USD files
 *   POST /api/scene       {path}      → load a scene
 *   GET  /api/hierarchy?path=...      → prim children at given path
 *   POST /api/pick        {x, y}      → pixel → prim path
 *   GET  /api/bookmarks               → list saved bookmarks
 *   POST /api/bookmarks   {name}      → save current camera as bookmark
 *   POST /api/bookmarks/recall/{name} → recall a bookmark
 *   DEL  /api/bookmarks/{name}        → delete a bookmark
 */

import type {
  Scene,
  HierarchyResponse,
  PickResponse,
  SceneInfo,
  BookmarksResponse,
  PrimProperties,
  PrimNode,
  VariantSet,
  BBox,
  MeasureResult,
  TimelineState,
  RenderMode,
  TelemetryPrim,
  TelemetryChannel,
} from '../types'

// ─── helper ──────────────────────────────────────────────────────────────────

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`API ${path} → ${res.status} ${res.statusText}`)
  }
  return res.json() as Promise<T>
}

// ─── scenes ──────────────────────────────────────────────────────────────────

export async function getScenes(): Promise<Scene[]> {
  const data = await apiFetch<{ scenes: Scene[] }>('/api/scenes')
  return data.scenes
}

/** POST /api/scene  { path: "relative/path.usd" } */
export async function loadScene(path: string): Promise<void> {
  await apiFetch<unknown>('/api/scene', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

// ─── hierarchy ───────────────────────────────────────────────────────────────

export async function getHierarchy(primPath: string = '/'): Promise<HierarchyResponse> {
  const encoded = encodeURIComponent(primPath)
  return apiFetch<HierarchyResponse>(`/api/hierarchy?path=${encoded}`)
}

// ─── pick ────────────────────────────────────────────────────────────────────

export async function pickAtPixel(x: number, y: number): Promise<PickResponse> {
  return apiFetch<PickResponse>('/api/pick', {
    method: 'POST',
    body: JSON.stringify({ x, y }),
  })
}

// ─── status / info ───────────────────────────────────────────────────────────

/** GET /api/status — returns scene state, prim count, camera angles */
export async function getSceneInfo(): Promise<SceneInfo> {
  return apiFetch<SceneInfo>('/api/status')
}

// ─── bookmarks ───────────────────────────────────────────────────────────────

export async function getBookmarks(): Promise<BookmarksResponse> {
  return apiFetch<BookmarksResponse>('/api/bookmarks')
}

/** POST /api/bookmarks  { name } — saves current camera position */
export async function saveBookmark(name: string): Promise<void> {
  await apiFetch<unknown>('/api/bookmarks', {
    method: 'POST',
    body: JSON.stringify({ name }),
  })
}

/** POST /api/bookmarks/recall/{name} — restores a saved camera position */
export async function recallBookmark(name: string): Promise<void> {
  await apiFetch<unknown>(`/api/bookmarks/recall/${encodeURIComponent(name)}`, {
    method: 'POST',
  })
}

/** DELETE /api/bookmarks/{name} */
export async function deleteBookmark(name: string): Promise<void> {
  await apiFetch<unknown>(`/api/bookmarks/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
}

// ─── camera ──────────────────────────────────────────────────────────────────

export async function sendCamera(payload: Record<string, unknown>): Promise<void> {
  await fetch('/camera', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

// ─── Prim properties ────────────────────────────────────────────────────────

/** GET /api/prim?path=... — read prim type, xform, visibility, attrs */
export async function getPrimProperties(primPath: string): Promise<PrimProperties> {
  const encoded = encodeURIComponent(primPath)
  return apiFetch<PrimProperties>(`/api/prim?path=${encoded}`)
}

/** POST /api/prim/xform — write transform to USD and reload (slow ~2s) */
export async function setPrimXform(
  path: string,
  translate?: [number, number, number],
  rotate?:    [number, number, number],
  scale?:     [number, number, number],
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>('/api/prim/xform', {
    method: 'POST',
    body: JSON.stringify({ path, translate, rotate, scale }),
  })
}

/** POST /api/prim/visibility — toggle prim visibility and reload */
export async function setPrimVisibility(path: string, visible: boolean): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>('/api/prim/visibility', {
    method: 'POST',
    body: JSON.stringify({ path, visible }),
  })
}

/** POST /api/save — export stage to output_path on the server */
export async function saveStage(outputPath: string): Promise<{ ok: boolean; path: string }> {
  return apiFetch<{ ok: boolean; path: string }>('/api/save', {
    method: 'POST',
    body: JSON.stringify({ output_path: outputPath }),
  })
}

// ─── Prim extensions ────────────────────────────────────────────────────────

/** GET /api/search?q=... — search prims by name/type */
export async function searchPrims(query: string): Promise<{ results: PrimNode[] }> {
  return apiFetch<{ results: PrimNode[] }>(`/api/search?q=${encodeURIComponent(query)}`)
}

/** GET /api/prim/variants?path=... — variant sets for a prim */
export async function getPrimVariants(path: string): Promise<{ variant_sets: VariantSet[] }> {
  return apiFetch<{ variant_sets: VariantSet[] }>(`/api/prim/variants?path=${encodeURIComponent(path)}`)
}

/** POST /api/prim/variant — select a variant (triggers stage reload) */
export async function setPrimVariant(path: string, variantSet: string, variant: string): Promise<{ ok: boolean }> {
  return apiFetch('/api/prim/variant', {
    method: 'POST',
    body: JSON.stringify({ path, variant_set: variantSet, variant }),
  })
}

/** GET /api/prim/bbox?path=... — world-space bounding box */
export async function getPrimBBox(path: string): Promise<BBox> {
  return apiFetch(`/api/prim/bbox?path=${encodeURIComponent(path)}`)
}

/** POST /api/measure — distance between two prim bbox centers */
export async function measureDistance(pathA: string, pathB: string): Promise<MeasureResult> {
  return apiFetch('/api/measure', {
    method: 'POST',
    body: JSON.stringify({ path_a: pathA, path_b: pathB }),
  })
}

/** GET /api/timeline */
export async function getTimeline(): Promise<TimelineState> {
  return apiFetch('/api/timeline')
}

/** POST /api/timeline — control playback */
export async function setTimeline(params: { time_code?: number; playing?: boolean; speed?: number }): Promise<TimelineState> {
  return apiFetch('/api/timeline', {
    method: 'POST',
    body: JSON.stringify(params),
  })
}

/** GET /api/render/mode */
export async function getRenderMode(): Promise<{ mode: RenderMode }> {
  return apiFetch('/api/render/mode')
}

/** POST /api/render/mode */
export async function setRenderMode(mode: RenderMode): Promise<{ ok: boolean; mode: RenderMode }> {
  return apiFetch('/api/render/mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
  })
}

// ─── Session layer authoring ────────────────────────────────────────────────

/** POST /api/create_prim — author a new prim in the session layer */
export async function createPrim(type: string, name: string): Promise<{ ok: boolean }> {
  return apiFetch('/api/create_prim', {
    method: 'POST',
    body: JSON.stringify({ type, name }),
  })
}

/** POST /api/deactivate_prim — set active=false on a prim (reversible via undo) */
export async function deactivatePrim(path: string): Promise<{ ok: boolean }> {
  return apiFetch('/api/deactivate_prim', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

/** POST /api/undo — revert the last session layer edit */
export async function undoAction(): Promise<{ ok: boolean }> {
  return apiFetch('/api/undo', { method: 'POST', body: '{}' })
}

/** POST /api/redo — re-apply the last undone edit */
export async function redoAction(): Promise<{ ok: boolean }> {
  return apiFetch('/api/redo', { method: 'POST', body: '{}' })
}

/** Trigger a snapshot download. Opens /api/snapshot in a hidden <a> element. */
export function downloadSnapshot(): void {
  const a = document.createElement('a')
  a.href     = '/api/snapshot'
  a.download = `snapshot_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.png`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

// ─── Telemetry simulation ───────────────────────────────────────────────────

/** GET /api/telemetry/prims — discover Xformable prims in the current scene */
export async function getTelemetryPrims(): Promise<TelemetryPrim[]> {
  const res = await apiFetch<{ prims: TelemetryPrim[] }>('/api/telemetry/prims')
  return res.prims ?? []
}

/** POST /api/telemetry/generate — bake animation and start playback */
export async function generateTelemetry(
  channels: TelemetryChannel[],
  duration: number,
  fps: number,
): Promise<{ ok: boolean; error?: string }> {
  return apiFetch('/api/telemetry/generate', {
    method: 'POST',
    body:   JSON.stringify({
      bindings: channels.map(({ id: _id, ...rest }) => rest),
      duration,
      fps,
    }),
  })
}

/** POST /api/telemetry/stop — stop playback and reload original scene */
export async function stopTelemetry(): Promise<{ ok: boolean }> {
  return apiFetch('/api/telemetry/stop', { method: 'POST', body: '{}' })
}
