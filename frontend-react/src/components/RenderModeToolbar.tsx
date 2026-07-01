/**
 * RenderModeToolbar — small button group overlaid on the viewport.
 *
 * RTX is the only supported mode in the ovrtx pip renderer.
 * Unlit and Wireframe require a Kit-based renderer and are shown
 * as disabled with an explanatory tooltip.
 */

import React from 'react'
import type { RenderMode } from '../types'

interface Props {
  activeScene: string | null
}

const MODES: { key: RenderMode; label: string; title: string; supported: boolean }[] = [
  { key: 'rtx',       label: 'RTX',  title: 'Full path-traced rendering (active)',                       supported: true  },
  { key: 'unlit',     label: 'Unlit', title: 'Flat shading — requires Kit renderer (not available)',     supported: false },
  { key: 'wireframe', label: 'Wire',  title: 'Wireframe — requires Kit renderer (not available)',        supported: false },
]

export default function RenderModeToolbar({ activeScene }: Props) {
  if (!activeScene) return null

  return (
    <div style={toolbar}>
      <div style={{ display: 'flex', gap: '2px' }}>
        {MODES.map(m => (
          <button
            key={m.key}
            title={m.title}
            disabled={!m.supported}
            style={{
              ...modeBtn,
              background:  m.supported ? '#76b900' : '#1e1e1e',
              color:       m.supported ? '#000'    : '#555',
              cursor:      m.supported ? 'default' : 'not-allowed',
              borderColor: m.supported ? '#76b900' : '#2a2a2a',
            }}
          >
            {m.label}
            {!m.supported && <span style={{ marginLeft: '3px', fontSize: '9px' }}>—</span>}
          </button>
        ))}
      </div>
      <div style={hint}>ovrtx pip: RTX only</div>
    </div>
  )
}

// ── styles ────────────────────────────────────────────────────────────────────

const toolbar: React.CSSProperties = {
  position:       'absolute',
  top:            '10px',
  right:          '10px',
  display:        'flex',
  flexDirection:  'column',
  alignItems:     'flex-end',
  gap:            '4px',
  zIndex:         10,
  pointerEvents:  'auto',
}
const hint: React.CSSProperties = {
  fontSize:   '9px',
  color:      '#444',
  textAlign:  'right',
  whiteSpace: 'nowrap',
}
const modeBtn: React.CSSProperties = {
  border:       '1px solid #333',
  borderRadius: '4px',
  padding:      '4px 8px',
  fontSize:     '11px',
  fontWeight:   600,
  transition:   'background 0.15s',
}
