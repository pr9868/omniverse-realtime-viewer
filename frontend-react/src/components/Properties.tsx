/**
 * Properties — shows the selected prim's type, transform, visibility, and
 * authored attributes with editable fields for the transform and visibility.
 *
 * Interaction flow:
 *   1. Parent passes selectedPrim whenever pick or hierarchy click changes it.
 *   2. useEffect fires on selectedPrim change → GET /api/prim?path=...
 *   3. User edits translate/rotate/scale inputs, clicks Apply.
 *   4. POST /api/prim/xform → server writes to USD + reloads stage (~2 s).
 *   5. Visibility toggle → POST /api/prim/visibility → same reload.
 *   6. Save As → POST /api/save { output_path } → export to new file.
 *
 * React concepts used:
 *   - Controlled inputs: each number input's value is bound to React state.
 *     When you type, onChange updates state; state drives the displayed value.
 *   - Optimistic UI: we show the new values immediately while the server call
 *     is in flight, then refresh from the server response on completion.
 */

import React, { useState, useEffect } from 'react'
import {
  getPrimProperties, setPrimXform, setPrimVisibility, saveStage,
  getPrimVariants, setPrimVariant, getPrimBBox, deactivatePrim,
} from '../api/client'
import type { PrimProperties, VariantSet, BBox } from '../types'

interface Props {
  selectedPrim: string | null
  activeScene:  string | null
  onEdit?:      () => void   // called after any edit that changes the hierarchy
}

type Vec3 = [number, number, number]

export default function Properties({ selectedPrim, activeScene, onEdit }: Props) {
  const [props,    setProps]    = useState<PrimProperties | null>(null)
  const [loading,  setLoading]  = useState(false)
  const [applying, setApplying] = useState(false)
  const [status,   setStatus]   = useState<string | null>(null)

  // Local editable copies of the xform values
  const [translate, setTranslate] = useState<Vec3>([0, 0, 0])
  const [rotate,    setRotate]    = useState<Vec3>([0, 0, 0])
  const [scale,     setScale]     = useState<Vec3>([1, 1, 1])
  const [visible,   setVisible]   = useState(true)

  // Variants
  const [variantSets, setVariantSets] = useState<VariantSet[]>([])

  // Bounding box
  const [bbox,      setBBox]      = useState<BBox | null>(null)
  const [showBBox,  setShowBBox]  = useState(false)

  // Save-as dialog state
  const [saveAsPath, setSaveAsPath] = useState('')
  const [showSaveAs, setShowSaveAs] = useState(false)

  // ── Fetch prim properties when selectedPrim changes ─────────────────────
  useEffect(() => {
    if (!selectedPrim || !activeScene) {
      setProps(null)
      return
    }
    setLoading(true)
    setStatus(null)
    setVariantSets([])
    setBBox(null)
    setShowBBox(false)
    getPrimProperties(selectedPrim)
      .then(p => {
        setProps(p)
        if (!p.error) {
          setTranslate(p.translate)
          setRotate(p.rotate)
          setScale(p.scale)
          setVisible(p.visibility !== 'invisible')
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false))
    // Fetch variants in parallel
    getPrimVariants(selectedPrim)
      .then(r => setVariantSets(r.variant_sets))
      .catch(() => {})
  }, [selectedPrim, activeScene])

  // ── Apply transform ─────────────────────────────────────────────────────
  async function handleApplyXform() {
    if (!selectedPrim) return
    setApplying(true)
    setStatus('Applying… (stage reloads)')
    try {
      const res = await setPrimXform(selectedPrim, translate, rotate, scale)
      setStatus(res.ok ? '✓ Transform applied' : '✗ Apply failed — check server logs')
      if (res.ok) {
        // Re-fetch updated properties
        const updated = await getPrimProperties(selectedPrim)
        setProps(updated)
      }
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
    } finally {
      setApplying(false)
    }
  }

  // ── Toggle visibility ───────────────────────────────────────────────────
  async function handleVisibilityToggle(newVisible: boolean) {
    if (!selectedPrim) return
    setVisible(newVisible)  // optimistic
    setApplying(true)
    setStatus('Toggling visibility…')
    try {
      const res = await setPrimVisibility(selectedPrim, newVisible)
      setStatus(res.ok ? '✓ Visibility updated' : '✗ Update failed')
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
      setVisible(!newVisible)  // revert on error
    } finally {
      setApplying(false)
    }
  }

  // ── Save As ─────────────────────────────────────────────────────────────
  async function handleSaveAs() {
    const path = saveAsPath.trim()
    if (!path) return
    setApplying(true)
    setStatus('Exporting…')
    try {
      const res = await saveStage(path)
      setStatus(res.ok ? `✓ Saved to ${res.path}` : '✗ Save failed')
      if (res.ok) setShowSaveAs(false)
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
    } finally {
      setApplying(false)
    }
  }

  // ── Variant select ──────────────────────────────────────────────────────
  async function handleVariant(vsName: string, variant: string) {
    if (!selectedPrim) return
    setApplying(true)
    setStatus(`Switching variant "${variant}"…`)
    try {
      const res = await setPrimVariant(selectedPrim, vsName, variant)
      setStatus(res.ok ? `✓ Variant "${variant}" applied` : '✗ Variant switch failed')
      if (res.ok) {
        // Refresh variants and props
        const [p, v] = await Promise.all([
          getPrimProperties(selectedPrim),
          getPrimVariants(selectedPrim),
        ])
        setProps(p)
        setVariantSets(v.variant_sets)
      }
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
    } finally {
      setApplying(false)
    }
  }

  // ── Bounding box ────────────────────────────────────────────────────────
  async function handleShowBBox() {
    if (!selectedPrim) return
    if (showBBox) { setShowBBox(false); return }
    try {
      const b = await getPrimBBox(selectedPrim)
      setBBox(b)
      setShowBBox(true)
    } catch { setBBox(null) }
  }

  // ── Deactivate (remove from scene, reversible via Undo) ──────────────
  async function handleDeactivate() {
    if (!selectedPrim) return
    if (!window.confirm(`Deactivate "${selectedPrim}"?\nThis removes it from the viewport. Use Ctrl+Z to undo.`)) return
    setApplying(true)
    setStatus('Deactivating…')
    try {
      const res = await deactivatePrim(selectedPrim)
      setStatus(res.ok ? '✓ Deactivated (Ctrl+Z to undo)' : '✗ Deactivate failed')
      if (res.ok) onEdit?.()
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
    } finally {
      setApplying(false)
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────
  if (!activeScene) return <div style={hint}>Load a scene first</div>
  if (!selectedPrim) return (
    <div style={{ padding: '12px' }}>
      <div style={{ color: '#555', fontSize: '12px', marginBottom: '10px' }}>
        Select a prim to inspect it.
      </div>
      <div style={{ color: '#444', fontSize: '11px', lineHeight: 1.6 }}>
        • Click a prim in the <strong style={{ color: '#666' }}>Hierarchy</strong> tab<br/>
        • Or click on an object in the viewport<br/><br/>
        You can then edit its <strong style={{ color: '#666' }}>transform</strong>, toggle <strong style={{ color: '#666' }}>visibility</strong>, switch <strong style={{ color: '#666' }}>variants</strong>, and measure its <strong style={{ color: '#666' }}>bounding box</strong>.
      </div>
    </div>
  )
  if (loading) return <div style={hint}>Loading properties…</div>
  if (!props) return null
  if (props.error) return <div style={{ ...hint, color: '#ff6b6b' }}>{props.error}</div>

  return (
    <div style={{ padding: '12px', overflowY: 'auto' }}>

      {/* Status message */}
      {status && (
        <div style={{
          marginBottom: '10px', padding: '6px 8px', borderRadius: '4px', fontSize: '11px',
          background: status.startsWith('✓') ? '#1a2e0a' : status.startsWith('✗') ? '#2e0a0a' : '#1a1a2e',
          color: status.startsWith('✓') ? '#76b900' : status.startsWith('✗') ? '#ff6b6b' : '#8888ff',
          border: `1px solid ${status.startsWith('✓') ? '#2a4a0a' : status.startsWith('✗') ? '#4a0a0a' : '#2a2a4e'}`,
        }}>
          {status}
        </div>
      )}

      {/* Prim identity */}
      <div style={sectionLabel}>Prim</div>
      <div style={{ fontFamily: 'monospace', fontSize: '11px', color: '#ccc', wordBreak: 'break-all', marginBottom: '4px' }}>
        {props.path}
      </div>
      <div style={{ fontSize: '11px', color: '#888', marginBottom: '12px' }}>
        Type: <span style={{ color: '#76b900' }}>{props.type}</span>
      </div>

      {/* Visibility toggle */}
      <div style={sectionLabel}>Visibility</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <input
          type="checkbox"
          id="vis-toggle"
          checked={visible}
          disabled={applying}
          onChange={e => handleVisibilityToggle(e.target.checked)}
          style={{ cursor: 'pointer', accentColor: '#76b900' }}
        />
        <label htmlFor="vis-toggle" style={{ fontSize: '12px', color: '#ccc', cursor: 'pointer' }}>
          {visible ? 'Visible' : 'Hidden'}
        </label>
      </div>

      {/* Transform — only shown for Xformable prims */}
      {props.has_xform && (
        <>
          <div style={sectionLabel}>Transform</div>
          <Vec3Row label="Translate" value={translate} onChange={setTranslate} disabled={applying} />
          <Vec3Row label="Rotate °"  value={rotate}    onChange={setRotate}    disabled={applying} />
          <Vec3Row label="Scale"     value={scale}     onChange={setScale}     disabled={applying} />
          <button
            onClick={handleApplyXform}
            disabled={applying}
            style={{ ...applyBtn, opacity: applying ? 0.5 : 1, marginBottom: '14px' }}
          >
            {applying ? 'Applying…' : 'Apply Transform'}
          </button>
        </>
      )}

      {/* Variants */}
      {variantSets.length > 0 && (
        <>
          <div style={sectionLabel}>Variants</div>
          {variantSets.map(vs => (
            <div key={vs.name} style={{ marginBottom: '8px' }}>
              <div style={{ fontSize: '11px', color: '#888', marginBottom: '3px' }}>{vs.name}</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                {vs.choices.map(choice => (
                  <button
                    key={choice}
                    onClick={() => handleVariant(vs.name, choice)}
                    disabled={applying}
                    style={{
                      ...variantChip,
                      background:  choice === vs.current ? '#76b900' : '#2a2a2a',
                      color:       choice === vs.current ? '#000'    : '#ccc',
                      borderColor: choice === vs.current ? '#76b900' : '#444',
                    }}
                  >
                    {choice}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </>
      )}

      {/* Bounding Box */}
      <div style={{ marginBottom: '8px' }}>
        <button onClick={handleShowBBox} style={secondaryBtn}>
          {showBBox ? 'Hide Bounding Box' : 'Show Bounding Box'}
        </button>
        {showBBox && bbox && !bbox.error && (
          <div style={{ fontSize: '11px', marginTop: '6px' }}>
            <BBoxRow label="Center"     value={bbox.center} />
            <BBoxRow label="Dimensions" value={bbox.dimensions} />
            <BBoxRow label="Min"        value={bbox.min} />
            <BBoxRow label="Max"        value={bbox.max} />
          </div>
        )}
      </div>

      {/* Deactivate Prim */}
      <div style={{ marginBottom: '12px' }}>
        <button
          onClick={handleDeactivate}
          disabled={applying}
          style={{ ...secondaryBtn, color: '#ff6b6b', borderColor: '#ff6b6b' }}
        >
          Deactivate Prim
        </button>
      </div>

      {/* Save As */}
      <div style={sectionLabel}>Export Stage</div>
      {!showSaveAs ? (
        <button onClick={() => setShowSaveAs(true)} style={secondaryBtn}>
          Save As…
        </button>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '12px' }}>
          <input
            value={saveAsPath}
            onChange={e => setSaveAsPath(e.target.value)}
            placeholder="/home/ubuntu/assets/edited.usda"
            style={textInput}
          />
          <div style={{ display: 'flex', gap: '6px' }}>
            <button onClick={handleSaveAs} disabled={applying} style={{ ...applyBtn, flex: 1 }}>
              Save
            </button>
            <button onClick={() => setShowSaveAs(false)} style={{ ...secondaryBtn, flex: 1 }}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Authored attributes (read-only, collapsible) */}
      {props.attrs.length > 0 && (
        <>
          <div style={{ ...sectionLabel, marginTop: '14px' }}>Authored Attributes</div>
          <div style={{ fontSize: '11px' }}>
            {props.attrs.map(attr => (
              <div key={attr.name} style={attrRow}>
                <span style={{ color: '#76b900', flexShrink: 0, marginRight: '6px' }}>{attr.name}</span>
                <span style={{ color: '#888', flexShrink: 0, marginRight: '6px' }}>{attr.type}</span>
                <span style={{ color: '#bbb', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {attr.value}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ── BBox display row ──────────────────────────────────────────────────────────

function BBoxRow({ label, value }: { label: string; value: [number, number, number] }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0', borderBottom: '1px solid #1e1e1e' }}>
      <span style={{ color: '#666' }}>{label}</span>
      <span style={{ fontFamily: 'monospace', color: '#bbb' }}>
        {value.map(v => v.toFixed(1)).join(', ')} cm
      </span>
    </div>
  )
}

// ── Vec3 row — three number inputs labeled X / Y / Z ─────────────────────────

interface Vec3RowProps {
  label:    string
  value:    Vec3
  onChange: (v: Vec3) => void
  disabled: boolean
}

function Vec3Row({ label, value, onChange, disabled }: Vec3RowProps) {
  function set(idx: 0 | 1 | 2, raw: string) {
    const n = parseFloat(raw)
    if (isNaN(n)) return
    const next = [...value] as Vec3
    next[idx] = n
    onChange(next)
  }

  return (
    <div style={{ marginBottom: '8px' }}>
      <div style={{ fontSize: '10px', color: '#666', marginBottom: '3px' }}>{label}</div>
      <div style={{ display: 'flex', gap: '4px' }}>
        {(['X', 'Y', 'Z'] as const).map((axis, i) => (
          <div key={axis} style={{ flex: 1 }}>
            <div style={{ fontSize: '9px', color: '#555', textAlign: 'center', marginBottom: '1px' }}>{axis}</div>
            <input
              type="number"
              step="0.1"
              value={value[i]}
              disabled={disabled}
              onChange={e => set(i as 0|1|2, e.target.value)}
              style={numInput}
            />
          </div>
        ))}
      </div>
    </div>
  )
}

// ── styles ────────────────────────────────────────────────────────────────────

const hint: React.CSSProperties = {
  padding: '12px',
  color:   '#555',
  fontSize:'12px',
}
const sectionLabel: React.CSSProperties = {
  fontSize:      '10px',
  letterSpacing: '0.08em',
  color:         '#76b900',
  fontWeight:    600,
  marginBottom:  '6px',
  textTransform: 'uppercase',
}
const numInput: React.CSSProperties = {
  width:        '100%',
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '3px',
  color:        '#e0e0e0',
  padding:      '3px 5px',
  fontSize:     '11px',
  textAlign:    'right',
  boxSizing:    'border-box',
}
const textInput: React.CSSProperties = {
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '4px',
  color:        '#e0e0e0',
  padding:      '5px 8px',
  fontSize:     '11px',
  width:        '100%',
  boxSizing:    'border-box',
}
const applyBtn: React.CSSProperties = {
  background:   '#76b900',
  color:        '#000',
  border:       'none',
  borderRadius: '4px',
  padding:      '5px 10px',
  cursor:       'pointer',
  fontSize:     '12px',
  fontWeight:   600,
  display:      'block',
  width:        '100%',
}
const secondaryBtn: React.CSSProperties = {
  background:   'none',
  color:        '#76b900',
  border:       '1px solid #76b900',
  borderRadius: '4px',
  padding:      '5px 10px',
  cursor:       'pointer',
  fontSize:     '12px',
  marginBottom: '12px',
  display:      'block',
  width:        '100%',
}
const variantChip: React.CSSProperties = {
  border:       '1px solid',
  borderRadius: '12px',
  padding:      '2px 10px',
  fontSize:     '11px',
  cursor:       'pointer',
  fontWeight:   500,
}
const attrRow: React.CSSProperties = {
  display:      'flex',
  padding:      '3px 0',
  borderBottom: '1px solid #1e1e1e',
  overflow:     'hidden',
}
