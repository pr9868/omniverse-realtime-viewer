/**
 * App — root component and state owner.
 *
 * Layout: three columns
 *   ┌──────────────┬─────────────────────────────┬──────────────┐
 *   │  SceneList   │         Viewport             │  Inspector   │
 *   │  (240 px)    │    (fills remaining space)   │  (280 px)    │
 *   └──────────────┴─────────────────────────────┴──────────────┘
 *
 * State that lives here (shared across columns):
 *   - activeScene: which USD file is currently loaded
 *   - selectedPrim: the USD path that was last picked/clicked
 */

import { useState, useCallback } from 'react'
import SceneList  from './components/SceneList'
import Viewport   from './components/Viewport'
import Inspector  from './components/Inspector'

const styles: Record<string, React.CSSProperties> = {
  layout: {
    display:       'flex',
    flexDirection: 'row',
    height:        '100vh',
    width:         '100vw',
    background:    '#1a1a1a',
    color:         '#e0e0e0',
    fontFamily:    '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize:      '13px',
    overflow:      'hidden',
  },
  sidebar: {
    width:        '240px',
    flexShrink:   0,
    borderRight:  '1px solid #333',
    display:      'flex',
    flexDirection:'column',
    overflow:     'hidden',
  },
  center: {
    flex:         1,
    display:      'flex',
    flexDirection:'column',
    overflow:     'hidden',
    position:     'relative',
  },
  inspector: {
    width:        '280px',
    flexShrink:   0,
    borderLeft:   '1px solid #333',
    display:      'flex',
    flexDirection:'column',
    overflow:     'hidden',
  },
}

export default function App() {
  // Which scene is currently loaded on the server (null = nothing loaded yet)
  const [activeScene, setActiveScene] = useState<string | null>(null)

  // Which USD prim path is currently selected (from picking or hierarchy click)
  const [selectedPrim, setSelectedPrim] = useState<string | null>(null)

  // useCallback memoises the function so child components don't re-render
  // unnecessarily when the parent re-renders for unrelated reasons.
  const handleSceneLoad = useCallback((scenePath: string) => {
    setActiveScene(scenePath)
    setSelectedPrim(null)  // clear selection when scene changes
  }, [])

  const handlePrimSelect = useCallback((primPath: string) => {
    setSelectedPrim(primPath)
  }, [])

  return (
    <div style={styles.layout}>
      {/* LEFT — scene list */}
      <aside style={styles.sidebar}>
        <SceneList
          activeScene={activeScene}
          onSceneLoad={handleSceneLoad}
        />
      </aside>

      {/* CENTER — WebRTC viewport */}
      <main style={styles.center}>
        <Viewport
          activeScene={activeScene}
          onPrimSelect={handlePrimSelect}
        />
      </main>

      {/* RIGHT — inspector panel */}
      <aside style={styles.inspector}>
        <Inspector
          activeScene={activeScene}
          selectedPrim={selectedPrim}
          onPrimSelect={handlePrimSelect}
        />
      </aside>
    </div>
  )
}
