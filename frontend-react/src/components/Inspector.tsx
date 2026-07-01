/**
 * Inspector — right panel.
 *
 * Four tabs:
 *   Hierarchy  — USD prim tree, lazy-loaded
 *   Selection  — details about the currently selected prim
 *   Bookmarks  — save / recall / delete camera positions
 *   Info       — prim count, stage path, camera angles
 *
 * React concept: conditional rendering.
 *   {condition && <Component />} renders Component only when condition is true.
 *   This is how tabs work — only one tab's content is rendered at a time.
 */

import React, { useState, useEffect, useCallback } from 'react'
import HierarchyTree from './HierarchyTree'
import Properties from './Properties'
import Telemetry from './Telemetry'
import { getHierarchy, getSceneInfo, getBookmarks, saveBookmark, recallBookmark, deleteBookmark, searchPrims, measureDistance, createPrim, undoAction, redoAction } from '../api/client'
import type { PrimNode, SceneInfo, Bookmark, MeasureResult, PrimType } from '../types'

interface Props {
  activeScene:  string | null
  selectedPrim: string | null
  onPrimSelect: (path: string) => void
}

type Tab = 'hierarchy' | 'properties' | 'measure' | 'bookmarks' | 'info' | 'telemetry'

export default function Inspector({ activeScene, selectedPrim, onPrimSelect }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>('hierarchy')

  // Auto-switch to properties tab when a prim is selected.
  // Exception: stay on measure tab so the user can fill targets.
  useEffect(() => {
    if (selectedPrim) {
      setActiveTab(prev => prev === 'measure' ? 'measure' : 'properties')
    }
  }, [selectedPrim])

  // ── Create prim state ─────────────────────────────────────────────────
  const [newPrimType, setNewPrimType] = useState<PrimType>('Sphere')
  const [newPrimName, setNewPrimName] = useState('')
  const [creating,   setCreating]    = useState(false)

  // ── Undo/redo state ───────────────────────────────────────────────────
  const [undoing, setUndoing] = useState(false)
  const [redoing, setRedoing] = useState(false)

  // ── Search state ─────────────────────────────────────────────────────────
  const [searchQ,       setSearchQ]       = useState('')
  const [searchResults, setSearchResults] = useState<PrimNode[]>([])
  const [searchLoading, setSearchLoading] = useState(false)

  // ── Measure state ─────────────────────────────────────────────────────────
  const [measureA,      setMeasureA]      = useState<string>('')
  const [measureB,      setMeasureB]      = useState<string>('')
  const [measureResult, setMeasureResult] = useState<MeasureResult | null>(null)
  const [measuring,     setMeasuring]     = useState(false)

  // Refs so the auto-fill effect always reads the latest values without
  // re-triggering on every keystroke.
  const measureARef = React.useRef(measureA)
  const measureBRef = React.useRef(measureB)
  useEffect(() => { measureARef.current = measureA }, [measureA])
  useEffect(() => { measureBRef.current = measureB }, [measureB])

  // When the selected prim changes while on the Measure tab, fill the next
  // empty slot automatically (A first, then B).
  useEffect(() => {
    if (!selectedPrim || activeTab !== 'measure') return
    if (!measureARef.current) {
      setMeasureA(selectedPrim)
    } else if (!measureBRef.current && selectedPrim !== measureARef.current) {
      setMeasureB(selectedPrim)
    }
  }, [selectedPrim, activeTab])

  // ── Hierarchy state ─────────────────────────────────────────────────────
  const [roots,    setRoots]    = useState<PrimNode[]>([])
  const [hierLoading, setHierLoading] = useState(false)

  // Re-fetch hierarchy whenever a new scene loads
  useEffect(() => {
    if (!activeScene) { setRoots([]); return }
    setHierLoading(true)
    getHierarchy('/')
      .then(res => setRoots(res.children))
      .catch(console.error)
      .finally(() => setHierLoading(false))
  }, [activeScene])

  // ── Scene info state ────────────────────────────────────────────────────
  const [info,     setInfo]     = useState<SceneInfo | null>(null)

  useEffect(() => {
    if (!activeScene) { setInfo(null); return }
    getSceneInfo().then(setInfo).catch(console.error)
  }, [activeScene])

  // ── Bookmark state ──────────────────────────────────────────────────────
  const [bookmarks,  setBookmarks]  = useState<Bookmark[]>([])
  const [newBmName,  setNewBmName]  = useState('')

  useEffect(() => {
    if (!activeScene) return
    getBookmarks().then(r => setBookmarks(r.bookmarks)).catch(console.error)
  }, [activeScene])

  async function handleSaveBookmark() {
    const name = newBmName.trim()
    if (!name) return
    await saveBookmark(name)
    setNewBmName('')
    getBookmarks().then(r => setBookmarks(r.bookmarks)).catch(console.error)
  }

  async function handleRecall(name: string) {
    await recallBookmark(name)
  }

  async function handleDelete(name: string) {
    await deleteBookmark(name)
    getBookmarks().then(r => setBookmarks(r.bookmarks)).catch(console.error)
  }

  // ── Hierarchy refresh ─────────────────────────────────────────────────────
  const refreshHierarchy = useCallback(() => {
    if (!activeScene) return
    getHierarchy('/').then(res => setRoots(res.children)).catch(console.error)
  }, [activeScene])

  // ── Create prim ───────────────────────────────────────────────────────────
  async function handleCreatePrim() {
    const name = newPrimName.trim()
    if (!name || !activeScene || creating) return
    setCreating(true)
    try {
      const res = await createPrim(newPrimType, name)
      if (res.ok) {
        setNewPrimName('')
        refreshHierarchy()
      }
    } catch { /* non-fatal */ }
    finally { setCreating(false) }
  }

  // ── Undo / Redo ───────────────────────────────────────────────────────────
  async function handleUndo() {
    if (undoing || !activeScene) return
    setUndoing(true)
    try {
      await undoAction()
      refreshHierarchy()
    } catch { /* non-fatal */ }
    finally { setUndoing(false) }
  }

  async function handleRedo() {
    if (redoing || !activeScene) return
    setRedoing(true)
    try {
      await redoAction()
      refreshHierarchy()
    } catch { /* non-fatal */ }
    finally { setRedoing(false) }
  }

  // Keyboard listener for Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!activeScene) return
      const mod = e.ctrlKey || e.metaKey
      if (mod && e.key === 'z' && !e.shiftKey) { e.preventDefault(); handleUndo() }
      if (mod && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) { e.preventDefault(); handleRedo() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [activeScene])  // eslint-disable-line react-hooks/exhaustive-deps

  // ── Search ────────────────────────────────────────────────────────────────
  async function handleSearch(q: string) {
    setSearchQ(q)
    if (!q.trim() || !activeScene) { setSearchResults([]); return }
    setSearchLoading(true)
    try {
      const r = await searchPrims(q)
      setSearchResults(r.results)
    } catch { setSearchResults([]) }
    finally { setSearchLoading(false) }
  }

  // ── Measure ───────────────────────────────────────────────────────────────
  async function handleMeasure() {
    if (!measureA || !measureB) return
    setMeasuring(true)
    setMeasureResult(null)
    try {
      const r = await measureDistance(measureA, measureB)
      setMeasureResult(r)
    } catch { setMeasureResult({ distance: 0, center_a: [0,0,0], center_b: [0,0,0], unit: 'cm', error: 'Failed' }) }
    finally { setMeasuring(false) }
  }

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Tab bar */}
      <div style={tabBar}>
        {(['hierarchy', 'properties', 'telemetry', 'measure', 'bookmarks', 'info'] as Tab[]).map(tab => (
          <button
            key={tab}
            onClick={() => {
              setActiveTab(tab)
              // Pre-fill measureA from the current selection when opening Measure tab
              if (tab === 'measure' && selectedPrim) {
                setMeasureA(prev => prev || selectedPrim)
              }
            }}
            style={{
              ...tabBtn,
              borderBottom: activeTab === tab ? '2px solid #76b900' : '2px solid transparent',
              color:        activeTab === tab ? '#76b900' : '#888',
            }}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div style={{ flex: 1, overflowY: 'auto' }}>

        {/* ── Hierarchy ────────────────────────────────────────────────── */}
        {activeTab === 'hierarchy' && (
          <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>

            {/* ── Create prim panel ─────────────────────────────────────── */}
            {activeScene && (
              <div style={{ padding: '6px 8px', borderBottom: '1px solid #2a2a2a', flexShrink: 0 }}>
                <div style={{ display: 'flex', gap: '4px', marginBottom: '4px' }}>
                  <select
                    value={newPrimType}
                    onChange={e => setNewPrimType(e.target.value as PrimType)}
                    style={selectStyle}
                  >
                    {(['Sphere', 'Cube', 'Cylinder', 'Xform', 'DomeLight'] as PrimType[]).map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                  <input
                    value={newPrimName}
                    onChange={e => setNewPrimName(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleCreatePrim()}
                    placeholder="Name…"
                    style={{ ...searchInput, flex: 1 }}
                  />
                  <button
                    onClick={handleCreatePrim}
                    disabled={creating || !newPrimName.trim()}
                    title="Add prim to scene"
                    style={{
                      ...createBtn,
                      opacity: (creating || !newPrimName.trim()) ? 0.4 : 1,
                    }}
                  >
                    {creating ? '…' : '+'}
                  </button>
                </div>
                {/* Undo / Redo */}
                <div style={{ display: 'flex', gap: '4px' }}>
                  <button onClick={handleUndo} disabled={undoing} style={undoRedoBtn} title="Undo (Ctrl+Z)">
                    ↩ Undo
                  </button>
                  <button onClick={handleRedo} disabled={redoing} style={undoRedoBtn} title="Redo (Ctrl+Y)">
                    ↪ Redo
                  </button>
                </div>
              </div>
            )}

            {/* Search bar */}
            {activeScene && (
              <div style={{ padding: '6px 8px', borderBottom: '1px solid #2a2a2a', flexShrink: 0 }}>
                <input
                  value={searchQ}
                  onChange={e => handleSearch(e.target.value)}
                  placeholder="Search prims…"
                  style={{ ...searchInput }}
                />
              </div>
            )}
            {/* Search results */}
            {searchQ && (
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {searchLoading && <div style={hint}>Searching…</div>}
                {!searchLoading && searchResults.length === 0 && <div style={hint}>No results</div>}
                {searchResults.map(r => (
                  <div
                    key={r.path}
                    onClick={() => { onPrimSelect(r.path); setSearchQ(''); setSearchResults([]) }}
                    style={{
                      ...searchResultRow,
                      background: r.path === selectedPrim ? '#2a3a1a' : 'transparent',
                    }}
                  >
                    <span style={{ color: '#76b900', fontSize: '10px' }}>{r.type}</span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.name}</span>
                    <span style={{ color: '#444', fontSize: '10px', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.path}</span>
                  </div>
                ))}
              </div>
            )}
            {/* Normal hierarchy when not searching */}
            {!searchQ && (
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {!activeScene && <div style={hint}>Load a scene first</div>}
                {hierLoading  && <div style={hint}>Loading hierarchy…</div>}
                {!hierLoading && activeScene && (
                  <HierarchyTree
                    roots={roots}
                    selectedPrim={selectedPrim}
                    onSelect={onPrimSelect}
                  />
                )}
              </div>
            )}
          </div>
        )}

        {/* ── Properties ───────────────────────────────────────────────── */}
        {activeTab === 'properties' && (
          <Properties selectedPrim={selectedPrim} activeScene={activeScene} onEdit={refreshHierarchy} />
        )}

        {/* ── Measure ──────────────────────────────────────────────────── */}
        {activeTab === 'measure' && (
          <div style={{ padding: '12px' }}>
            {!activeScene
              ? <div style={hint}>Load a scene first</div>
              : (
                <>
                  <div style={sectionLabel}>Measure Distance</div>
                  <div style={{ fontSize: '11px', color: '#666', marginBottom: '10px' }}>
                    Select prims in the Hierarchy tab — they auto-fill here. Or type paths directly.
                  </div>

                  <div style={{ marginBottom: '6px' }}>
                    <div style={{ fontSize: '10px', color: '#888', marginBottom: '2px' }}>Prim A</div>
                    <div style={{ display: 'flex', gap: '4px' }}>
                      <input value={measureA} onChange={e => setMeasureA(e.target.value)}
                        placeholder="/World/PrimA" style={{ ...input, flex: 1 }} />
                      {selectedPrim && (
                        <button onClick={() => setMeasureA(selectedPrim)} style={smallBtn} title="Use selected">⊙</button>
                      )}
                    </div>
                  </div>

                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ fontSize: '10px', color: '#888', marginBottom: '2px' }}>Prim B</div>
                    <div style={{ display: 'flex', gap: '4px' }}>
                      <input value={measureB} onChange={e => setMeasureB(e.target.value)}
                        placeholder="/World/PrimB" style={{ ...input, flex: 1 }} />
                      {selectedPrim && selectedPrim !== measureA && (
                        <button onClick={() => setMeasureB(selectedPrim)} style={smallBtn} title="Use selected">⊙</button>
                      )}
                    </div>
                  </div>

                  <button
                    onClick={handleMeasure}
                    disabled={!measureA || !measureB || measuring}
                    style={{ ...actionBtn, width: '100%', marginBottom: '12px', opacity: (!measureA || !measureB) ? 0.4 : 1 }}
                  >
                    {measuring ? 'Measuring…' : 'Measure'}
                  </button>

                  {measureResult && !measureResult.error && (
                    <div style={{ background: '#1a2e0a', border: '1px solid #2a4a0a', borderRadius: '6px', padding: '10px' }}>
                      <div style={{ fontSize: '22px', fontWeight: 700, color: '#76b900', textAlign: 'center' }}>
                        {measureResult.distance.toFixed(1)} cm
                      </div>
                      <div style={{ fontSize: '11px', color: '#888', textAlign: 'center', marginTop: '2px' }}>
                        ({(measureResult.distance / 100).toFixed(3)} m)
                      </div>
                      <div style={{ marginTop: '8px', fontSize: '11px' }}>
                        <div style={{ color: '#555', marginBottom: '2px' }}>A: {measureResult.center_a.map(v => v.toFixed(1)).join(', ')}</div>
                        <div style={{ color: '#555' }}>B: {measureResult.center_b.map(v => v.toFixed(1)).join(', ')}</div>
                      </div>
                    </div>
                  )}
                  {measureResult?.error && (
                    <div style={{ color: '#ff6b6b', fontSize: '11px' }}>{measureResult.error}</div>
                  )}
                </>
              )
            }
          </div>
        )}

        {/* ── Bookmarks ────────────────────────────────────────────────── */}
        {activeTab === 'bookmarks' && (
          <div style={{ padding: '12px' }}>
            {!activeScene
              ? <div style={hint}>Load a scene first</div>
              : (
                <>
                  {/* Save new bookmark */}
                  <div style={sectionLabel}>Save Camera Position</div>
                  <div style={{ display: 'flex', gap: '6px', marginBottom: '12px' }}>
                    <input
                      value={newBmName}
                      onChange={e => setNewBmName(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && handleSaveBookmark()}
                      placeholder="Bookmark name…"
                      style={input}
                    />
                    <button onClick={handleSaveBookmark} style={actionBtn}>Save</button>
                  </div>

                  {/* Bookmark list */}
                  <div style={sectionLabel}>Saved Positions</div>
                  {bookmarks.length === 0 && <div style={hint}>No bookmarks yet</div>}
                  {bookmarks.map(bm => (
                    <div key={bm.name} style={bmRow}>
                      <span style={{ flex: 1, fontSize: '12px' }}>{bm.name}</span>
                      <button onClick={() => handleRecall(bm.name)} style={smallBtn} title="Recall">↩</button>
                      <button onClick={() => handleDelete(bm.name)} style={{ ...smallBtn, color: '#ff6b6b' }} title="Delete">✕</button>
                    </div>
                  ))}
                </>
              )
            }
          </div>
        )}

        {/* ── Info ─────────────────────────────────────────────────────── */}
        {activeTab === 'info' && (
          <div style={{ padding: '12px' }}>
            {!info
              ? <div style={hint}>{activeScene ? 'Loading…' : 'Load a scene first'}</div>
              : (
                <>
                  <div style={sectionLabel}>Scene</div>
                  <InfoRow label="File"       value={info.scene?.split('/').pop() ?? '—'} />
                  <InfoRow label="State"      value={info.state} />
                  <InfoRow label="Prim count" value={String(info.prim_count)} />

                  <div style={{ ...sectionLabel, marginTop: '12px' }}>Camera</div>
                  <InfoRow label="Azimuth"   value={`${info.cam_az_deg}°`} />
                  <InfoRow label="Elevation" value={`${info.cam_el_deg}°`} />
                  <InfoRow label="Distance"  value={`${info.cam_r.toFixed(0)} cm`} />
                </>
              )
            }
          </div>
        )}

        {/* ── Telemetry ─────────────────────────────────────────────────── */}
        {activeTab === 'telemetry' && (
          <Telemetry activeScene={activeScene} />
        )}

      </div>
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', fontSize: '12px' }}>
      <span style={{ color: '#888' }}>{label}</span>
      <span>{value}</span>
    </div>
  )
}

// ── styles ────────────────────────────────────────────────────────────────────
const tabBar: React.CSSProperties = {
  display:      'flex',
  borderBottom: '1px solid #333',
  flexShrink:   0,
}
const tabBtn: React.CSSProperties = {
  flex:       1,
  padding:    '8px 4px',
  background: 'none',
  border:     'none',
  cursor:     'pointer',
  fontSize:   '11px',
  fontWeight: 500,
}
const hint: React.CSSProperties = {
  padding: '12px',
  color:   '#555',
  fontSize:'12px',
}
const sectionLabel: React.CSSProperties = {
  fontSize:    '10px',
  letterSpacing:'0.08em',
  color:       '#76b900',
  fontWeight:  600,
  marginBottom:'6px',
  textTransform:'uppercase',
}
const searchInput: React.CSSProperties = {
  width:        '100%',
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '4px',
  color:        '#e0e0e0',
  padding:      '4px 8px',
  fontSize:     '12px',
  boxSizing:    'border-box' as const,
}
const searchResultRow: React.CSSProperties = {
  display:      'flex',
  flexDirection:'column' as const,
  padding:      '5px 10px',
  borderBottom: '1px solid #1e1e1e',
  cursor:       'pointer',
  gap:          '1px',
}
const input: React.CSSProperties = {
  flex:       1,
  background: '#2a2a2a',
  border:     '1px solid #444',
  borderRadius:'4px',
  color:      '#e0e0e0',
  padding:    '4px 8px',
  fontSize:   '12px',
}
const actionBtn: React.CSSProperties = {
  background:   '#76b900',
  color:        '#000',
  border:       'none',
  borderRadius: '4px',
  padding:      '4px 10px',
  cursor:       'pointer',
  fontSize:     '12px',
  fontWeight:   600,
}
const bmRow: React.CSSProperties = {
  display:     'flex',
  alignItems:  'center',
  gap:         '4px',
  padding:     '4px 0',
  borderBottom:'1px solid #2a2a2a',
}
const smallBtn: React.CSSProperties = {
  background: 'none',
  border:     'none',
  color:      '#888',
  cursor:     'pointer',
  fontSize:   '13px',
  padding:    '2px 4px',
}
const selectStyle: React.CSSProperties = {
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '4px',
  color:        '#ccc',
  fontSize:     '11px',
  padding:      '3px 4px',
  cursor:       'pointer',
}
const createBtn: React.CSSProperties = {
  background:   '#76b900',
  color:        '#000',
  border:       'none',
  borderRadius: '4px',
  padding:      '3px 10px',
  fontSize:     '14px',
  fontWeight:   700,
  cursor:       'pointer',
  flexShrink:   0,
}
const undoRedoBtn: React.CSSProperties = {
  flex:         1,
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '4px',
  color:        '#888',
  fontSize:     '11px',
  padding:      '3px 6px',
  cursor:       'pointer',
}
