/**
 * SceneList — left sidebar.
 *
 * On mount, fetches /api/scenes and shows the list.
 * Clicking a scene calls /api/load and notifies the parent (App) via onSceneLoad.
 *
 * React concept used here: useEffect
 *   useEffect(() => { ... }, [])  runs ONCE after the component first renders.
 *   It's where you put "fetch data when the component appears" logic.
 *   The empty [] dependency array means "only run this on mount, not on every render".
 */

import { useEffect, useState } from 'react'
import { getScenes, loadScene } from '../api/client'
import type { Scene } from '../types'

interface Props {
  activeScene: string | null
  onSceneLoad: (path: string) => void
}

export default function SceneList({ activeScene, onSceneLoad }: Props) {
  const [scenes,  setScenes]  = useState<Scene[]>([])
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState<string | null>(null)
  const [loadingScene, setLoadingScene] = useState<string | null>(null)

  // Fetch scene list once on mount
  useEffect(() => {
    getScenes()
      .then(setScenes)
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  async function handleClick(scene: Scene) {
    if (loadingScene) return   // ignore clicks while a load is in progress
    setLoadingScene(scene.path)
    try {
      await loadScene(scene.path)
      onSceneLoad(scene.path)
    } catch (e) {
      setError(`Failed to load: ${String(e)}`)
    } finally {
      setLoadingScene(null)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div style={header}>
        <span style={{ fontWeight: 600, letterSpacing: '0.05em', color: '#76b900' }}>
          SCENES
        </span>
        <button onClick={() => {
          setLoading(true)
          getScenes().then(setScenes).catch(e => setError(String(e))).finally(() => setLoading(false))
        }} style={refreshBtn} title="Refresh scene list">↺</button>
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
        {loading && <div style={hint}>Loading scenes…</div>}
        {error   && <div style={{ ...hint, color: '#ff6b6b' }}>{error}</div>}
        {!loading && !error && scenes.length === 0 && (
          <div style={hint}>No scenes found in asset root</div>
        )}
        {scenes.map(scene => {
          const isActive  = scene.path === activeScene
          const isLoading = scene.path === loadingScene
          return (
            <button
              key={scene.path}
              onClick={() => handleClick(scene)}
              style={{
                ...sceneBtn,
                background: isActive ? '#2a3a1a' : 'transparent',
                borderLeft: isActive ? '3px solid #76b900' : '3px solid transparent',
                color:      isActive ? '#76b900' : '#ccc',
                opacity:    isLoading ? 0.6 : 1,
              }}
              title={scene.path}
            >
              {isLoading ? '⏳ ' : '🗂 '}{scene.name}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── styles ────────────────────────────────────────────────────────────────────
// Inline styles keep everything in one file while we're learning.
// A real app would use CSS modules or a design system.

const header: React.CSSProperties = {
  display:        'flex',
  justifyContent: 'space-between',
  alignItems:     'center',
  padding:        '10px 12px',
  borderBottom:   '1px solid #333',
  flexShrink:     0,
}
const refreshBtn: React.CSSProperties = {
  background: 'none',
  border:     'none',
  color:      '#888',
  fontSize:   '16px',
  cursor:     'pointer',
  padding:    '0 4px',
}
const hint: React.CSSProperties = {
  padding: '12px',
  color:   '#666',
  fontSize:'12px',
}
const sceneBtn: React.CSSProperties = {
  display:     'block',
  width:       '100%',
  textAlign:   'left',
  padding:     '8px 12px',
  border:      'none',
  cursor:      'pointer',
  fontSize:    '12px',
  transition:  'background 0.1s',
  whiteSpace:  'nowrap',
  overflow:    'hidden',
  textOverflow:'ellipsis',
}
