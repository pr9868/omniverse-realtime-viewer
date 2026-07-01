// The SDK exposes window.OVWebRTC, from which you destructure AppStreamer and StreamType.
// Usage: const { AppStreamer, StreamType } = window.OVWebRTC

interface AppStreamerConnectOptions {
  streamSource: number   // StreamType.DIRECT = 0
  logLevel?: number
  streamConfig: {
    videoElementId:  string
    audioElementId?: string
    server:          string   // EC2 IP
    signalingPort:   number   // 49100
    fps?:            number
    maxReconnects?:  number
    onStart?:        (msg: { action: string; status: string; info?: string }) => void
    onStop?:         (msg: Record<string, unknown>) => void
    onUpdate?:       (msg: Record<string, unknown>) => void
  }
}

interface AppStreamerStatic {
  connect(options: AppStreamerConnectOptions): Promise<void>
  terminate(): Promise<void>
}

interface OVWebRTCStatic {
  AppStreamer: AppStreamerStatic
  StreamType:  { DIRECT: number }
}

declare global {
  interface Window {
    OVWebRTC: OVWebRTCStatic
  }
}

export {}
