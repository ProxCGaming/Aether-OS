import React from 'react';
import { useAetherSocket, destructiveActionLabel } from '../contexts/SocketContext';

// Wave 2 slice 4 (audit Lane A, QUEUE-class caution): the
// reconfirmation prompt for queued DESTRUCTIVE actions. While the
// engine was unreachable, actions in SocketContext's destructive set
// (settings reset, memory clear, provider remove, cancel task) were
// queued like everything else — but unlike everything else they do
// not flush on reconnect. The provider diverts them to its held set,
// and this prompt is where the user decides, per action or all at
// once, whether each one still goes out. Discarding drops an action
// permanently. There is deliberately no dismiss button: a prompt
// that could be hidden with the actions still held would strand
// them invisibly, so the prompt stays until every held action is
// sent or discarded. Placement follows the app-level notice idiom of
// PendingQueueIndicator — fixed, above the shell, bottom-left —
// stacked just above that pill, but interactive and in the amber
// "your call is needed" treatment rather than the pill's passive one.
export default function HeldActionsPrompt() {
  const { heldMessages, confirmHeld, discardHeld } = useAetherSocket();

  if (!heldMessages || heldMessages.length === 0) return null;

  const sendBtn = {
    background: 'rgba(251, 191, 36, 0.18)',
    border: '1px solid rgba(251, 191, 36, 0.5)',
    color: '#fbbf24',
    fontSize: '12px',
    padding: '4px 10px',
    borderRadius: '8px',
    cursor: 'pointer',
    fontWeight: 500,
  };
  const discardBtn = {
    background: 'transparent',
    border: '1px solid rgba(255, 255, 255, 0.18)',
    color: '#b8b8c4',
    fontSize: '12px',
    padding: '4px 10px',
    borderRadius: '8px',
    cursor: 'pointer',
  };

  return (
    <div
      role="alertdialog"
      aria-label="Confirm queued actions"
      data-testid="held-actions-prompt"
      style={{
        position: 'fixed',
        left: '20px',
        bottom: '64px',
        zIndex: 300,
        width: '340px',
        maxWidth: 'calc(100vw - 40px)',
        background: 'rgba(28, 22, 8, 0.97)',
        border: '1px solid rgba(251, 191, 36, 0.45)',
        borderRadius: '14px',
        padding: '14px 16px',
        color: '#f0f0f5',
        boxShadow: '0 12px 32px rgba(0, 0, 0, 0.45)',
        backdropFilter: 'blur(8px)',
      }}
    >
      <div style={{ fontSize: '13px', fontWeight: 600, color: '#fbbf24', marginBottom: '6px' }}>
        Queued while offline — confirm before sending
      </div>
      <p style={{ fontSize: '12px', color: '#c9c9d4', margin: '0 0 10px 0', lineHeight: 1.45 }}>
        These actions were queued while the engine was unreachable and have <strong>not</strong> been
        sent. They stay paused here until you send or discard each one.
      </p>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {heldMessages.map((held, index) => (
          <li
            key={index}
            data-testid="held-action-row"
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px' }}
          >
            <span style={{ fontSize: '13px' }}>{destructiveActionLabel(held)}</span>
            <span style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
              <button type="button" style={sendBtn} onClick={() => confirmHeld(index)}>
                Send
              </button>
              <button type="button" style={discardBtn} onClick={() => discardHeld(index)}>
                Discard
              </button>
            </span>
          </li>
        ))}
      </ul>
      {heldMessages.length > 1 && (
        <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
          <button type="button" style={sendBtn} onClick={() => confirmHeld()}>
            Send all
          </button>
          <button type="button" style={discardBtn} onClick={() => discardHeld()}>
            Discard all
          </button>
        </div>
      )}
    </div>
  );
}
