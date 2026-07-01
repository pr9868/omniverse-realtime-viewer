/**
 * TimelineBar — animation playback controls at the bottom of the viewport.
 *
 * Shows play/pause, current time code, scrub slider, and speed control.
 * Only visible when the loaded stage has animation (has_anim = true).
 *
 * React concept: controlled vs uncontrolled inputs.
 *   The scrub slider is a controlled input — its value always comes from
 *   React state. When the user drags it, onChange fires, we call the API,
 *   and update state from the response so the slider stays in sync.
 */

import React, { useEffect, useRef, useState } from 'react'
import { getTimeline, setTimeline } from '../api/client'
import type { TimelineState } from '../types'

interface Props {
  activeScene: string | null
}

export default function TimelineBar({ activeScene }: Props) {
  const [tl,        setTl]        = useState<TimelineState | null>(null)
  const [dragging,  setDragging]  = useState(false)
  const [localTime, setLocalTime] = useState(0)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Poll timeline state while a scene is loaded ─────────────────────────
  useEffect(() => {
    if (!activeScene) { setTl(null); return }

    getTimeline().then(t => { setTl(t); setLocalTime(t.time_code) }).catch(() => {})

    pollRef.current = setInterval(() => {
      if (!dragging) {
        getTimeline().then(t => { setTl(t); setLocalTime(t.time_code) }).catch(() => {})
      }
    }, 250)

    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [activeScene, dragging])

  if (!tl || !tl.has_anim) return null   // hidden when no animation

  const { time_start, time_end, is_playing, speed } = tl

  async function handlePlayPause() {
    const t = await setTimeline({ playing: !is_playing })
    setTl(t)
  }

  async function handleScrubEnd(val: number) {
    setDragging(false)
    const t = await setTimeline({ time_code: val, playing: false })
    setTl(t)
    setLocalTime(t.time_code)
  }

  async function handleSpeedChange(val: number) {
    const t = await setTimeline({ speed: val })
    setTl(t)
  }

  return (
    <div style={bar}>
      {/* Play / Pause */}
      <button onClick={handlePlayPause} style={playBtn} title={is_playing ? 'Pause' : 'Play'}>
        {is_playing ? '⏸' : '▶'}
      </button>

      {/* Time display */}
      <span style={timeLabel}>{localTime.toFixed(1)}</span>

      {/* Scrub slider */}
      <input
        type="range"
        min={time_start}
        max={time_end}
        step={0.5}
        value={localTime}
        style={{ flex: 1, accentColor: '#76b900', cursor: 'pointer' }}
        onMouseDown={() => setDragging(true)}
        onChange={e => setLocalTime(parseFloat(e.target.value))}
        onMouseUp={e => handleScrubEnd(parseFloat((e.target as HTMLInputElement).value))}
      />

      {/* End time */}
      <span style={timeLabel}>{time_end.toFixed(0)}</span>

      {/* Speed selector */}
      <select
        value={speed}
        onChange={e => handleSpeedChange(parseFloat(e.target.value))}
        style={speedSelect}
        title="Playback speed"
      >
        {[0.25, 0.5, 1, 2, 4].map(s => (
          <option key={s} value={s}>{s}×</option>
        ))}
      </select>
    </div>
  )
}

// ── styles ────────────────────────────────────────────────────────────────────

const bar: React.CSSProperties = {
  display:        'flex',
  alignItems:     'center',
  gap:            '8px',
  padding:        '4px 10px',
  background:     '#111',
  borderTop:      '1px solid #2a2a2a',
  flexShrink:     0,
  height:         '36px',
}
const playBtn: React.CSSProperties = {
  background:   'none',
  border:       'none',
  color:        '#76b900',
  fontSize:     '16px',
  cursor:       'pointer',
  padding:      '0 4px',
  flexShrink:   0,
}
const timeLabel: React.CSSProperties = {
  fontFamily:  'monospace',
  fontSize:    '11px',
  color:       '#888',
  flexShrink:  0,
  minWidth:    '40px',
  textAlign:   'center',
}
const speedSelect: React.CSSProperties = {
  background:   '#1e1e1e',
  border:       '1px solid #333',
  borderRadius: '4px',
  color:        '#ccc',
  fontSize:     '11px',
  padding:      '2px 4px',
  cursor:       'pointer',
  flexShrink:   0,
}
