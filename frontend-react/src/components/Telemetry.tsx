import React, { useState, useCallback, useRef, useEffect } from 'react'
import type { TelemetryPrim, TelemetryChannel, ChannelType } from '../types'
import { getTelemetryPrims, generateTelemetry, stopTelemetry } from '../api/client'

// ─── constants ────────────────────────────────────────────────────────────────

const CHANNEL_LABELS: Record<ChannelType, string> = {
  oscillate_x: 'Oscillate X',
  oscillate_y: 'Oscillate Y',
  oscillate_z: 'Oscillate Z',
  rotate_z:    'Rotate Z',
  alert_pulse: 'Alert Pulse (flash)',
  linear:      'Linear (Conveyor)',
}

const CHANNEL_UNIT: Record<ChannelType, string> = {
  oscillate_x: 'cm',
  oscillate_y: 'cm',
  oscillate_z: 'cm',
  rotate_z:    '°',
  alert_pulse: 'Hz',
  linear:      'cm',   // amplitude = total travel distance
}

// ─── signal chart helpers ─────────────────────────────────────────────────────

const CHANNEL_COLORS: Record<ChannelType, string> = {
  oscillate_x: '#76b900',   // NVIDIA green
  oscillate_y: '#00bcd4',   // cyan
  oscillate_z: '#ff9800',   // orange
  rotate_z:    '#ce93d8',   // lavender
  alert_pulse: '#f44336',   // red
  linear:      '#ffd600',   // yellow
}

/** Scrolling time window shown in the chart (seconds) */
const TIME_WINDOW_S = 8

/**
 * Compute the instantaneous signal value for a channel at time t.
 * Mirrors the server's USDA generation math so the chart matches exactly
 * what is baked into the USD layer.
 */
function computeSignalValue(ch: TelemetryChannel, t: number, clipDuration: number): number {
  switch (ch.channel_type) {
    case 'oscillate_x':
    case 'oscillate_y':
    case 'oscillate_z':
    case 'rotate_z':
      return ch.amplitude * Math.sin(2 * Math.PI * ch.frequency * t)
    case 'alert_pulse': {
      const period = 1 / Math.max(ch.frequency, 0.01)
      const phase  = ((t % period) + period) % period   // always positive
      return phase < period / 2 ? ch.amplitude : 0
    }
    case 'linear': {
      const dur   = clipDuration > 0 ? clipDuration : 30
      const loopT = ((t % dur) + dur) % dur
      const tNorm = loopT / dur
      return ch.amplitude * (1 - Math.abs(2 * tNorm - 1))  // triangle wave: 0→amp→0
    }
    default:
      return 0
  }
}

/** Assign sensible defaults to the first N discovered prims */
function autoAssign(prims: TelemetryPrim[]): TelemetryChannel[] {
  const defaults: { channel_type: ChannelType; amplitude: number; frequency: number }[] = [
    { channel_type: 'rotate_z',    amplitude: 90,  frequency: 0.2  },  // slow spin — always visible
    { channel_type: 'oscillate_y', amplitude: 8,   frequency: 0.3  },  // gentle vertical bob
    { channel_type: 'rotate_z',    amplitude: 180, frequency: 0.15 },  // slow wide spin
    { channel_type: 'oscillate_y', amplitude: 5,   frequency: 0.5  },  // fast small bob
    { channel_type: 'rotate_z',    amplitude: 45,  frequency: 0.4  },  // medium tilt
  ]
  return prims.slice(0, defaults.length).map((prim, i) => ({
    id:           crypto.randomUUID(),
    enabled:      true,
    path:         prim.path,
    ...defaults[i],
  }))
}

// ─── styles ───────────────────────────────────────────────────────────────────

const card: React.CSSProperties = {
  background:   '#1a1a1a',
  border:       '1px solid #333',
  borderRadius: '6px',
  padding:      '10px 12px',
  marginBottom: '8px',
}

const label: React.CSSProperties = {
  fontSize: '11px',
  color:    '#888',
  display:  'block',
  marginBottom: '3px',
}

const select: React.CSSProperties = {
  width:       '100%',
  background:  '#111',
  color:       '#e0e0e0',
  border:      '1px solid #444',
  borderRadius:'4px',
  padding:     '3px 6px',
  fontSize:    '12px',
  marginBottom:'6px',
}

const numInput: React.CSSProperties = {
  width:       '70px',
  background:  '#111',
  color:       '#e0e0e0',
  border:      '1px solid #444',
  borderRadius:'4px',
  padding:     '3px 6px',
  fontSize:    '12px',
}

const primaryBtn: React.CSSProperties = {
  padding:      '6px 18px',
  background:   '#76b900',
  color:        '#000',
  border:       'none',
  borderRadius: '4px',
  fontWeight:   700,
  cursor:       'pointer',
  fontSize:     '12px',
}

const dangerBtn: React.CSSProperties = {
  padding:      '6px 18px',
  background:   'transparent',
  color:        '#ff6b6b',
  border:       '1px solid #ff6b6b',
  borderRadius: '4px',
  fontWeight:   700,
  cursor:       'pointer',
  fontSize:     '12px',
}

const secondaryBtn: React.CSSProperties = {
  padding:      '4px 12px',
  background:   'transparent',
  color:        '#aaa',
  border:       '1px solid #444',
  borderRadius: '4px',
  cursor:       'pointer',
  fontSize:     '11px',
}

// ─── component ────────────────────────────────────────────────────────────────

interface Props {
  activeScene: string | null
}

export default function Telemetry({ activeScene }: Props) {
  const [prims,     setPrims]     = useState<TelemetryPrim[]>([])
  const [channels,  setChannels]  = useState<TelemetryChannel[]>([])
  const [duration,  setDuration]  = useState<number>(30)
  const [fps,       setFps]       = useState<number>(24)
  const [active,    setActive]    = useState<boolean>(false)
  const [loading,   setLoading]   = useState<false | 'discover' | 'generate' | 'stop'>(false)
  const [status,    setStatus]    = useState<string>('')

  // ── discover prims ──────────────────────────────────────────────────────────
  const handleDiscover = useCallback(async () => {
    setLoading('discover')
    setStatus('Scanning scene for Xformable prims…')
    try {
      const found = await getTelemetryPrims()
      setPrims(found)
      setChannels(autoAssign(found))
      setStatus(`Found ${found.length} Xformable prims — ${Math.min(found.length, 5)} auto-assigned`)
    } catch (e) {
      setStatus(`✗ Discover failed: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [])

  // ── channel updates ─────────────────────────────────────────────────────────
  const updateChannel = useCallback((id: string, patch: Partial<TelemetryChannel>) => {
    setChannels(prev => prev.map(ch => ch.id === id ? { ...ch, ...patch } : ch))
  }, [])

  const addChannel = useCallback(() => {
    if (prims.length === 0) return
    setChannels(prev => [...prev, {
      id:           crypto.randomUUID(),
      enabled:      true,
      path:         prims[0].path,
      channel_type: 'oscillate_x',
      amplitude:    50,
      frequency:    0.5,
    }])
  }, [prims])

  const removeChannel = useCallback((id: string) => {
    setChannels(prev => prev.filter(ch => ch.id !== id))
  }, [])

  // ── generate + start ────────────────────────────────────────────────────────
  const handleStart = useCallback(async () => {
    const active = channels.filter(ch => ch.enabled)
    if (active.length === 0) { setStatus('Enable at least one channel first'); return }
    setLoading('generate')
    setStatus(`Generating ${duration}s animation clip (${active.length} channels)…`)
    try {
      const res = await generateTelemetry(active, duration, fps)
      if (res.ok) {
        setActive(true)
        setStatus(`✓ Playing — ${duration}s loop @ ${fps} fps`)
      } else {
        setStatus(`✗ ${res.error ?? 'Generate failed'}`)
      }
    } catch (e) {
      setStatus(`✗ Error: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [channels, duration, fps])

  // ── stop ────────────────────────────────────────────────────────────────────
  const handleStop = useCallback(async () => {
    setLoading('stop')
    setStatus('Stopping telemetry…')
    try {
      await stopTelemetry()
      setActive(false)
      setStatus('Stopped — original scene restored')
    } catch (e) {
      setStatus(`✗ Stop failed: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [])

  // ── signal chart ────────────────────────────────────────────────────────────
  const canvasRef   = useRef<HTMLCanvasElement>(null)
  const rafRef      = useRef<number>(0)
  const t0Ref       = useRef<number>(0)
  const liveChRef   = useRef<TelemetryChannel[]>(channels)
  const liveDurRef  = useRef<number>(duration)

  // Keep refs in sync with state (avoids stale closures in the animation loop)
  useEffect(() => { liveChRef.current = channels }, [channels])
  useEffect(() => { liveDurRef.current = duration }, [duration])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    cancelAnimationFrame(rafRef.current)

    const drawFrame = (elapsed: number) => {
      // Sync canvas pixel width to its CSS width every frame (handles resizes)
      const W = canvas.offsetWidth || 280
      if (canvas.width !== W) canvas.width = W
      canvas.height = 108
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const H  = canvas.height
      const ch = liveChRef.current.filter(c => c.enabled)
      const dur = liveDurRef.current

      // Background
      ctx.fillStyle = '#0c0c0c'
      ctx.fillRect(0, 0, W, H)

      // Subtle grid
      ctx.lineWidth = 1
      ctx.strokeStyle = '#191919'
      ctx.setLineDash([])
      for (let row = 1; row <= 3; row++) {
        const y = Math.round(H * row / 4) + 0.5
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke()
      }
      for (let col = 1; col <= 4; col++) {
        const x = Math.round(W * col / 5) + 0.5
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke()
      }
      // Centre baseline
      ctx.strokeStyle = '#282828'
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(0, H / 2 + 0.5); ctx.lineTo(W, H / 2 + 0.5); ctx.stroke()

      if (ch.length === 0) {
        ctx.fillStyle = '#3a3a3a'
        ctx.font = '10px monospace'
        ctx.textAlign = 'center'
        ctx.fillText('enable channels and click ▶ Start', W / 2, H / 2 + 4)
        return
      }

      // Waveforms
      ch.forEach((c, idx) => {
        const color  = CHANNEL_COLORS[c.channel_type]
        const tStart = elapsed >= TIME_WINDOW_S ? elapsed - TIME_WINDOW_S : 0

        ctx.strokeStyle = color
        ctx.lineWidth   = 1.5
        ctx.beginPath()
        for (let px = 0; px < W; px++) {
          const t    = tStart + (px / W) * Math.min(elapsed, TIME_WINDOW_S)
          const val  = computeSignalValue(c, t, dur)
          const norm = c.amplitude > 0 ? val / c.amplitude : 0  // –1 … +1
          const y    = H / 2 - norm * (H / 2 - 5)
          px === 0 ? ctx.moveTo(px, y) : ctx.lineTo(px, y)
        }
        ctx.stroke()

        // Live value tag (stacked top-right)
        const curVal = computeSignalValue(c, elapsed, dur)
        const unit   = CHANNEL_UNIT[c.channel_type]
        ctx.fillStyle = color
        ctx.font      = 'bold 9px monospace'
        ctx.textAlign = 'right'
        ctx.fillText(`${c.channel_type.replace('_', ' ')} ${curVal.toFixed(1)}${unit}`, W - 5, 10 + idx * 12)
      })
    }

    if (!active) {
      // Static preview when stopped
      drawFrame(TIME_WINDOW_S)
      return
    }

    t0Ref.current = performance.now()
    const loop = () => {
      drawFrame((performance.now() - t0Ref.current) / 1000)
      rafRef.current = requestAnimationFrame(loop)
    }
    loop()
    return () => cancelAnimationFrame(rafRef.current)
  }, [active])

  // ── render ──────────────────────────────────────────────────────────────────
  if (!activeScene) {
    return (
      <div style={{ padding: '24px 16px', color: '#666', fontSize: '12px', textAlign: 'center' }}>
        Load a scene first to use telemetry.
      </div>
    )
  }

  return (
    <div style={{ padding: '12px', fontSize: '12px', color: '#ccc' }}>

      {/* ── Header + controls ── */}
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '12px', flexWrap: 'wrap' }}>
        <button
          onClick={handleDiscover}
          disabled={!!loading || active}
          style={secondaryBtn}
        >
          🔍 Discover Prims
        </button>

        {!active ? (
          <button
            onClick={handleStart}
            disabled={!!loading || channels.length === 0}
            style={primaryBtn}
          >
            {loading === 'generate' ? 'Generating…' : '▶ Start'}
          </button>
        ) : (
          <button
            onClick={handleStop}
            disabled={loading === 'stop'}
            style={dangerBtn}
          >
            {loading === 'stop' ? 'Stopping…' : '■ Stop'}
          </button>
        )}

        {active && (
          <span style={{ color: '#76b900', fontSize: '11px', fontWeight: 700 }}>
            ● LIVE
          </span>
        )}
      </div>

      {/* ── Global settings ── */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '12px' }}>
        <div>
          <span style={label}>Duration (s)</span>
          <input
            type="number" min={5} max={300} step={5}
            value={duration}
            onChange={e => setDuration(Number(e.target.value))}
            disabled={active}
            style={numInput}
          />
        </div>
        <div>
          <span style={label}>FPS</span>
          <input
            type="number" min={12} max={60} step={12}
            value={fps}
            onChange={e => setFps(Number(e.target.value))}
            disabled={active}
            style={numInput}
          />
        </div>
      </div>

      {/* ── Status ── */}
      {status && (
        <div style={{
          fontSize: '11px', color: status.startsWith('✗') ? '#ff6b6b' : '#aaa',
          marginBottom: '10px', fontStyle: 'italic',
        }}>
          {status}
        </div>
      )}

      {/* ── Channel list ── */}
      {channels.length === 0 && prims.length === 0 && (
        <div style={{ color: '#555', fontSize: '11px', textAlign: 'center', padding: '16px 0' }}>
          Click "Discover Prims" to auto-assign channels,<br />or add channels manually below.
        </div>
      )}

      {channels.map(ch => (
        <div key={ch.id} style={card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: active ? 'default' : 'pointer' }}>
              <input
                type="checkbox"
                checked={ch.enabled}
                disabled={active}
                onChange={e => updateChannel(ch.id, { enabled: e.target.checked })}
                style={{ accentColor: '#76b900', width: '14px', height: '14px' }}
              />
              <span style={{ fontWeight: 600, color: ch.enabled ? '#e0e0e0' : '#555' }}>
                {CHANNEL_LABELS[ch.channel_type]}
              </span>
            </label>
            {!active && (
              <button
                onClick={() => removeChannel(ch.id)}
                style={{ ...secondaryBtn, padding: '1px 6px', color: '#ff6b6b', borderColor: '#ff6b6b' }}
              >
                ✕
              </button>
            )}
          </div>

          {/* Prim path selector */}
          <span style={label}>Prim</span>
          <select
            value={ch.path}
            onChange={e => updateChannel(ch.id, { path: e.target.value })}
            disabled={active}
            style={select}
          >
            {prims.length > 0
              ? prims.map(p => (
                  <option key={p.path} value={p.path}>{p.path} ({p.type})</option>
                ))
              : <option value={ch.path}>{ch.path}</option>
            }
          </select>

          {/* Channel type */}
          <span style={label}>Animation type</span>
          <select
            value={ch.channel_type}
            onChange={e => updateChannel(ch.id, { channel_type: e.target.value as ChannelType })}
            disabled={active}
            style={select}
          >
            {(Object.keys(CHANNEL_LABELS) as ChannelType[]).map(k => (
              <option key={k} value={k}>{CHANNEL_LABELS[k]}</option>
            ))}
          </select>

          {/* Amplitude + Frequency / Angle */}
          <div style={{ display: 'flex', gap: '16px' }}>
            <div>
              <span style={label}>
                {ch.channel_type === 'linear' ? 'Distance (cm)' : `Amplitude (${CHANNEL_UNIT[ch.channel_type]})`}
              </span>
              <input
                type="number" min={1} max={5000} step={ch.channel_type === 'linear' ? 50 : 5}
                value={ch.amplitude}
                onChange={e => updateChannel(ch.id, { amplitude: Number(e.target.value) })}
                disabled={active}
                style={numInput}
              />
            </div>
            <div>
              {ch.channel_type === 'linear' ? (
                <>
                  <span style={label}>Angle (°)</span>
                  <input
                    type="number" min={0} max={360} step={15}
                    value={ch.frequency}
                    onChange={e => updateChannel(ch.id, { frequency: Number(e.target.value) })}
                    disabled={active}
                    style={numInput}
                    title="0°=+X  90°=+Z  180°=-X  270°=-Z"
                  />
                </>
              ) : (
                <>
                  <span style={label}>Frequency (Hz)</span>
                  <input
                    type="number" min={0.05} max={2} step={0.05}
                    value={ch.frequency}
                    onChange={e => updateChannel(ch.id, { frequency: Number(e.target.value) })}
                    disabled={active}
                    style={numInput}
                  />
                </>
              )}
            </div>
          </div>
        </div>
      ))}

      {/* ── Add channel ── */}
      {!active && (
        <button
          onClick={addChannel}
          disabled={prims.length === 0}
          style={{ ...secondaryBtn, marginTop: '4px', width: '100%' }}
        >
          + Add channel
        </button>
      )}

      {/* ── Signal chart ── */}
      {channels.length > 0 && (
        <div style={{ marginTop: '14px' }}>
          <div style={{
            fontSize: '10px', color: '#555', marginBottom: '5px',
            letterSpacing: '0.6px', textTransform: 'uppercase',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          }}>
            <span>Signal Preview</span>
            {active && (
              <span style={{ color: '#f44336', fontWeight: 700, letterSpacing: 0 }}>
                ● LIVE · {TIME_WINDOW_S}s window
              </span>
            )}
          </div>
          <canvas
            ref={canvasRef}
            style={{
              width: '100%', height: '108px', display: 'block',
              borderRadius: '4px', border: '1px solid #1e1e1e',
            }}
          />
          {/* Legend */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '6px' }}>
            {channels.filter(c => c.enabled).map(c => (
              <span key={c.id} style={{ fontSize: '9px', color: CHANNEL_COLORS[c.channel_type], fontFamily: 'monospace' }}>
                ── {CHANNEL_LABELS[c.channel_type]}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
