import React from 'react';
import { MessageSquare } from 'lucide-react';
import EmptyState from './EmptyState';

// S-8 (FF1-10): the main window's designed detached state. While
// the chat lives in its own floating window, the Chat tab's
// content area shows this panel — what happened, whether a task
// is running there, and the primary Re-attach action — instead of
// the void that used to leave the small title-bar button as the
// only way back. Display only: re-attach runs the existing
// handleReattachChat path in App; no transfer mechanics live here.
export default function DetachedChatPanel({ isThinking = false, onReattach }) {
  return (
    <div
      data-testid="detached-chat-panel"
      style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px' }}
    >
      <div className="aether-content-panel" style={{ width: '100%', maxWidth: '520px', padding: '12px 20px 20px' }}>
        <EmptyState
          icon={<MessageSquare size={26} color="var(--text-muted)" />}
          title="Chat is in its own window"
          description={isThinking
            ? 'A task is running in the detached chat. It keeps running there — re-attach to bring the conversation back here.'
            : 'Your conversation is open in a separate window. Re-attach to bring it back into the main window.'}
          actionLabel="Re-attach chat"
          onAction={onReattach}
          testId="detached-chat-state"
        />
        <div
          data-testid="detached-chat-status"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', fontSize: '13px', color: 'var(--text-secondary)' }}
        >
          <span
            aria-hidden="true"
            style={{ width: '8px', height: '8px', borderRadius: '50%', background: isThinking ? 'var(--accent)' : 'var(--success)', flexShrink: 0 }}
          />
          {isThinking ? 'Task running in the detached chat' : 'Detached chat is idle'}
        </div>
      </div>
    </div>
  );
}
