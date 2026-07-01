/**
 * Viewport — center panel.
 *
 * Renders the WebRTC video stream from the server and handles:
 *   - AppStreamer initialization (on mount, once)
 *   - Click → pick query → prim selection
 *   - Connection status overlay
 *
 * React concepts used here:
 *
 *   useRef — a ref is a "box" that holds a value without causing re-renders.
 *     Used for the <video> DOM element (we need to hand it to AppStreamer)
 *     and for tracking drag state (we don't want re-renders on every mouse move).
 *
 *   useEffect — runs side effects after render.
 *     The AppStreamer.setup() call goes here because it needs the <video>
 *     element to exist in the DOM first — which it does after the first render.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import { pickAtPixel, sendCamera, downloadSnapshot } from '../api/client'
import RenderModeToolbar from './RenderModeToolbar'
import TimelineBar from './TimelineBar'

// The server IP is read from the environment variable set in .env.local
// import.meta.env is Vite's way of accessing env vars in the browser bundle.
const SERVER_IP = import.meta.env.VITE_SERVER_IP as string ?? 'localhost'

interface Props {
  activeScene: string | null
  onPrimSelect: (path: string) => void
}

type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

export default function Viewport({ activeScene, onPrimSelect }: Props) {
  const videoRef   = useRef<HTMLVideoElement>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  const [status, setStatus]     = useState<ConnectionStatus>('disconnected')
  const [picking, setPicking]   = useState(false)

  // ── Initialize AppStreamer once on mount ──────────────────────────────────
  useEffect(() => {
    if (!window.OVWebRTC) {
      console.error('OVWebRTC SDK not found — did index.html load the SDK?')
      setStatus('error')
      return
    }

    const { AppStreamer, StreamType } = window.OVWebRTC

    setStatus('connecting')

    AppStreamer.connect({
      streamSource: StreamType.DIRECT,
      logLevel: 2,
      streamConfig: {
        videoElementId: 'ov-stream-video',
        audioElementId: 'ov-stream-audio',
        server:         SERVER_IP,
        signalingPort:  49100,
        fps:            60,
        maxReconnects:  3,
        onStart: (msg) => {
          console.log('[AppStreamer onStart]', msg)
          if (msg.action !== 'start') return
          if (msg.status === 'success') {
            setStatus('connected')
            // Unmute and play — browsers require a user gesture before autoplay with sound,
            // but muted autoplay is allowed. We unmute after connection is confirmed.
            const video = document.getElementById('ov-stream-video') as HTMLVideoElement
            if (video) { video.muted = false; video.play().catch(() => {}); video.focus() }
          } else {
            setStatus('error')
          }
        },
        onStop: () => {
          console.log('[AppStreamer] stopped')
          setStatus('disconnected')
        },
        onUpdate: (msg) => {
          console.log('[AppStreamer onUpdate]', msg)
        },
      },
    }).catch(err => {
      console.error('[AppStreamer] connect failed:', err)
      setStatus('error')
    })

    // Cleanup: terminate when component unmounts
    return () => {
      window.OVWebRTC?.AppStreamer.terminate().catch(() => {})
    }
  }, [])  // [] = run once on mount

  // ── Scroll → zoom ────────────────────────────────────────────────────────
  // React's onWheel is passive by default (can't call preventDefault).
  // We need preventDefault to stop the page from scrolling while zooming,
  // so we attach a native DOM listener with { passive: false } instead.
  useEffect(() => {
    const el = wrapperRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      if (!activeScene) return
      sendCamera({ event_type: 'cameraZoom', payload: { delta: e.deltaY } }).catch(() => {})
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [activeScene])

  // ── Click → pick ─────────────────────────────────────────────────────────
  const handleClick = useCallback(async (e: React.MouseEvent<HTMLDivElement>) => {
    if (!activeScene || picking) return

    // Get click position relative to the video element
    const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect()
    const x = Math.round(e.clientX - rect.left)
    const y = Math.round(e.clientY - rect.top)

    // Scale to the render resolution (server renders at 1920×1080)
    const scaleX = 1920 / rect.width
    const scaleY = 1080 / rect.height
    const px = Math.round(x * scaleX)
    const py = Math.round(y * scaleY)

    setPicking(true)
    try {
      const result = await pickAtPixel(px, py)
      if (result.prim_path) {
        onPrimSelect(result.prim_path)
      }
    } catch (err) {
      console.warn('Pick failed:', err)
    } finally {
      setPicking(false)
    }
  }, [activeScene, picking, onPrimSelect])

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={container}>
      {/* Status bar */}
      <div style={statusBar}>
        <span style={{ color: statusColor(status) }}>● {status}</span>
        {activeScene && (
          <span style={{ color: '#888', marginLeft: '12px' }}>
            {activeScene.split('/').pop()}
          </span>
        )}
        {picking && <span style={{ color: '#76b900', marginLeft: 'auto' }}>picking…</span>}
        {activeScene && !picking && (
          <button
            onClick={downloadSnapshot}
            title="Download current rendered frame as PNG"
            style={snapshotBtn}
          >
            📷 Snapshot
          </button>
        )}
      </div>

      {/* Video — AppStreamer attaches the stream to this element by ID */}
      <div
        ref={wrapperRef}
        style={videoWrapper}
        onClick={handleClick}
      >
        <video
          id="ov-stream-video"
          ref={videoRef}
          style={videoStyle}
          autoPlay
          muted
          playsInline
        />
        <audio id="ov-stream-audio" autoPlay style={{ display: 'none' }} />

        {/* Render mode buttons — top right corner of viewport */}
        <RenderModeToolbar activeScene={activeScene} />

        {/* Overlay shown before stream starts */}
        {status !== 'connected' && (
          <div style={overlay}>
            {status === 'connecting' && <span>Connecting to EC2…</span>}
            {status === 'disconnected' && <span>Stream disconnected</span>}
            {status === 'error' && <span style={{ color: '#ff6b6b' }}>Connection failed — check EC2 is running</span>}
          </div>
        )}

        {!activeScene && status === 'connected' && (
          <div style={{ ...overlay, background: 'rgba(0,0,0,0.5)' }}>
            <span style={{ color: '#888' }}>Select a scene from the left panel</span>
          </div>
        )}
      </div>

      {/* Timeline bar — only visible when stage has animation */}
      <TimelineBar activeScene={activeScene} />
    </div>
  )
}

function statusColor(s: ConnectionStatus) {
  return { connected: '#76b900', connecting: '#f0a500', disconnected: '#666', error: '#ff6b6b' }[s]
}

// ── styles ────────────────────────────────────────────────────────────────────
const container: React.CSSProperties = {
  display:       'flex',
  flexDirection: 'column',
  height:        '100%',
}
const statusBar: React.CSSProperties = {
  display:        'flex',
  alignItems:     'center',
  padding:        '6px 12px',
  borderBottom:   '1px solid #333',
  fontSize:       '12px',
  flexShrink:     0,
  minHeight:      '30px',
}
const videoWrapper: React.CSSProperties = {
  flex:     1,
  position: 'relative',
  overflow: 'hidden',
  cursor:   'crosshair',
}
const videoStyle: React.CSSProperties = {
  width:      '100%',
  height:     '100%',
  objectFit:  'contain',
  background: '#000',
  display:    'block',
}
const snapshotBtn: React.CSSProperties = {
  marginLeft:      'auto',
  padding:         '3px 10px',
  fontSize:        '11px',
  background:      'transparent',
  border:          '1px solid #555',
  borderRadius:    '4px',
  color:           '#ccc',
  cursor:          'pointer',
}
const overlay: React.CSSProperties = {
  position:       'absolute',
  inset:          0,
  display:        'flex',
  alignItems:     'center',
  justifyContent: 'center',
  background:     'rgba(0,0,0,0.7)',
  color:          '#aaa',
  fontSize:       '14px',
}
