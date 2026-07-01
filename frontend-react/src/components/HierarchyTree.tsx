/**
 * HierarchyTree — recursive USD prim tree.
 *
 * Each TreeNode manages its own "expanded" state and lazily fetches children
 * from /api/hierarchy when first expanded.
 *
 * React concept: recursion with components.
 *   A TreeNode renders a list of child TreeNodes. This is the cleanest way
 *   to handle tree UIs in React — each node is an independent component with
 *   its own local state (expanded, loading, children).
 */

import { useState, useCallback, useEffect } from 'react'
import { getHierarchy } from '../api/client'
import type { PrimNode } from '../types'

// ── Top-level export ──────────────────────────────────────────────────────────

interface TreeProps {
  roots:          PrimNode[]
  selectedPrim:   string | null
  onSelect:       (path: string) => void
}

export default function HierarchyTree({ roots, selectedPrim, onSelect }: TreeProps) {
  if (roots.length === 0) {
    return <div style={emptyStyle}>No prims</div>
  }
  return (
    <div style={{ padding: '4px 0' }}>
      {roots.map(node => (
        <TreeNode
          key={node.path}
          node={node}
          depth={0}
          selectedPrim={selectedPrim}
          onSelect={onSelect}
        />
      ))}
    </div>
  )
}

// ── Individual node ───────────────────────────────────────────────────────────

interface NodeProps {
  node:         PrimNode
  depth:        number
  selectedPrim: string | null
  onSelect:     (path: string) => void
}

function TreeNode({ node, depth, selectedPrim, onSelect }: NodeProps) {
  // has_children comes from the server — true if this prim has any children.
  // We don't fetch them until the user clicks to expand (lazy loading).
  const hasChildren = node.has_children

  const [expanded,        setExpanded]        = useState(depth === 0)
  const [childNodes,      setChildNodes]      = useState<PrimNode[]>([])
  const [loadingChildren, setLoadingChildren] = useState(false)

  // Auto-fetch children if this node starts expanded (e.g. root node at depth 0)
  useEffect(() => {
    if (expanded && hasChildren && childNodes.length === 0) {
      setLoadingChildren(true)
      getHierarchy(node.path)
        .then(res => setChildNodes(res.children))
        .catch(() => {})
        .finally(() => setLoadingChildren(false))
    }
  }, [])  // intentionally runs once on mount only

  const isSelected = node.path === selectedPrim

  const handleToggle = useCallback(async () => {
    if (!hasChildren) return
    const next = !expanded
    setExpanded(next)

    // Lazy load: fetch children from API the first time we expand
    if (next && childNodes.length === 0) {
      setLoadingChildren(true)
      try {
        const res = await getHierarchy(node.path)
        setChildNodes(res.children)
      } catch {
        // silently fail — children just won't show
      } finally {
        setLoadingChildren(false)
      }
    }
  }, [expanded, hasChildren, childNodes.length, node.path])

  const indent = depth * 14  // pixels of left padding per depth level

  return (
    <div>
      {/* Row */}
      <div
        style={{
          ...row,
          paddingLeft: `${8 + indent}px`,
          background:  isSelected ? '#2a3a1a' : 'transparent',
          borderLeft:  isSelected ? '2px solid #76b900' : '2px solid transparent',
          color:       isSelected ? '#76b900' : '#ccc',
        }}
        onClick={() => onSelect(node.path)}
      >
        {/* Expand/collapse toggle */}
        <span
          style={{ ...toggle, visibility: hasChildren ? 'visible' : 'hidden' }}
          onClick={(e) => { e.stopPropagation(); handleToggle() }}
        >
          {loadingChildren ? '⏳' : expanded ? '▾' : '▸'}
        </span>

        {/* Prim type icon */}
        <span style={typeIcon}>{primIcon(node.type)}</span>

        {/* Prim name */}
        <span style={nameStyle} title={node.path}>{node.name}</span>

        {/* Type label */}
        <span style={typeLabel}>{node.type}</span>
      </div>

      {/* Children (only rendered when expanded) */}
      {expanded && childNodes.length > 0 && (
        <div>
          {childNodes.map(child => (
            <TreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              selectedPrim={selectedPrim}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ── helpers ───────────────────────────────────────────────────────────────────

function primIcon(type: string): string {
  const map: Record<string, string> = {
    Xform:    '📦',
    Mesh:     '🔷',
    Camera:   '📷',
    Light:    '💡',
    Scope:    '📁',
    Material: '🎨',
    Shader:   '✨',
  }
  return map[type] ?? '○'
}

// ── styles ────────────────────────────────────────────────────────────────────

const row: React.CSSProperties = {
  display:     'flex',
  alignItems:  'center',
  gap:         '4px',
  height:      '24px',
  cursor:      'pointer',
  fontSize:    '12px',
  userSelect:  'none',
  paddingRight:'8px',
}
const toggle: React.CSSProperties = {
  width:     '14px',
  flexShrink: 0,
  color:      '#888',
  fontSize:   '10px',
  cursor:     'pointer',
  textAlign:  'center',
}
const typeIcon: React.CSSProperties = {
  fontSize:  '11px',
  flexShrink: 0,
}
const nameStyle: React.CSSProperties = {
  flex:         1,
  overflow:     'hidden',
  textOverflow: 'ellipsis',
  whiteSpace:   'nowrap',
}
const typeLabel: React.CSSProperties = {
  color:      '#555',
  fontSize:   '10px',
  flexShrink:  0,
}
const emptyStyle: React.CSSProperties = {
  padding: '8px 12px',
  color:   '#555',
  fontSize:'12px',
}
