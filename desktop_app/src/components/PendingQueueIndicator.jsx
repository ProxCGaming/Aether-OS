import React from 'react';
import { useAetherSocket } from '../contexts/SocketContext';

// Wave 2 slice 1: the app-level pending indicator. While user actions
// sit in the socket facade's offline queue (sent via safeSend while
// the engine was unreachable), this pill says so in plain language.
// It is driven entirely by SocketContext's pendingCount — the real
// queue state, mirrored into React state by the provider — never by a
// component's local guess, and it disappears the moment the queue
// flushes on reconnect. Styling follows the app's amber notice idiom
// (the same treatment as Onboarding's "Queued — …" notices).
export default function PendingQueueIndicator() {
  const { pendingCount } = useAetherSocket();

  if (!pendingCount) return null;

  return (
    <div
      role="status"
      style={{
        position: 'fixed',
        left: '20px',
        bottom: '20px',
        zIndex: 200,
        background: 'rgba(251, 191, 36, 0.15)',
        border: '1px solid rgba(251, 191, 36, 0.35)',
        color: '#fbbf24',
        fontSize: '12px',
        padding: '6px 12px',
        borderRadius: '999px',
        pointerEvents: 'none',
        backdropFilter: 'blur(8px)',
      }}
    >
      {pendingCount} action{pendingCount === 1 ? '' : 's'} queued — will send when the engine reconnects
    </div>
  );
}
