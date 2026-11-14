import React from 'react';
import LoadStateRow from './LoadStateRow';

// Master build S11: Settings → Conversation.
//
// One switch, controlled in the E6-F05 sense like the Calendar
// panel: SettingsPanel owns the config and the one
// CONFIG_UPDATE flow; this panel only renders the applied value
// and reports edits, so a refused write (configError, surfaced
// verbatim) simply never lands and the switch keeps showing
// what is actually in force.
//
// The copy is the honest contract of aether_engine/eq.py: the
// signals are read locally from the current conversation,
// nothing about mood is stored in long-term memory, and Aether
// adapts its behaviour without ever claiming to know how the
// user feels. No other claim is made here, because no other
// behaviour exists.

export default function ConversationPanel({
  configAnswered = true,
  isConnected = true,
  configError = '',
  eqEnabled = true,
  onEqEnabledChange,
}) {
  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Conversation</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
          How Aether adapts the way it replies while you talk with it.
        </p>
      </div>

      {!configAnswered && (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          loadingText="Loading your saved conversation settings…"
          offlineText="Waiting for the engine — your saved conversation settings will appear when it reconnects."
          testId="conversation-config-load-state"
        />
      )}

      {configAnswered && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', background: 'var(--bg-base)', borderRadius: '12px', padding: '20px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '14px' }}>Adaptive replies</div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>
                Aether notices signals in the recent conversation — a question asked again, a correction,
                signs that something is not working, a request marked urgent — and adapts: slowing down and
                simplifying when things go wrong, explaining one step at a time when an answer did not land,
                keeping replies short when you are in a hurry. The signals are read on this machine from the
                current conversation only; nothing about your mood is stored in long-term memory, and Aether
                never claims to know how you feel. Turn this off and every reply uses the standard style.
              </div>
            </div>
            <input
              type="checkbox"
              data-testid="eq-enabled-toggle"
              checked={eqEnabled}
              onChange={(e) => onEqEnabledChange && onEqEnabledChange(e.target.checked)}
              style={{ accentColor: 'var(--accent)', width: '18px', height: '18px', flexShrink: 0 }}
            />
          </div>
          {configError ? (
            <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{configError}</div>
          ) : null}
        </div>
      )}
    </div>
  );
}
