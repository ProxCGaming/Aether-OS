import React, { useState, useEffect, useCallback } from 'react';
import { Globe, ShieldAlert, Wallet, Layers, KeyRound } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

// Epic 8, E8-6.2: Settings → Browser Agent Policy. The owner's
// confirmation policy for the agent browser: a master switch, the
// envelope + vault-autofill toggles, and one mode per consequential
// category (Always confirm / Allow with envelope / Allow freely).
// The engine is the authority — this panel only reads/writes the
// policy over BROWSER_POLICY_GET/SET; every change is a partial
// update and the SET response (the full resulting policy) is what
// gets rendered, so a rejected update visibly changes nothing.

const CATEGORIES = [
  {
    id: 'payments',
    label: 'Payments',
    desc: 'Checkouts, purchases, and entering payment card details.',
    note: 'Payments are always confirmed individually — envelopes never cover payments.',
  },
  {
    id: 'sends_publishes',
    label: 'Sends & publishes',
    desc: 'Sending messages or email; posting, publishing, or sharing content.',
  },
  {
    id: 'deletes',
    label: 'Deletes',
    desc: 'Deleting or removing anything on a site.',
  },
  {
    id: 'account_changes',
    label: 'Account changes',
    desc: 'Password, email, profile, and account-settings changes.',
  },
];

const MODES = [
  { value: 'always_confirm', label: 'Always confirm' },
  { value: 'allow_with_envelope', label: 'Allow with envelope' },
  { value: 'allow_freely', label: 'Allow freely' },
];

const PRESET = {
  enabled: true,
  vault_autofill: false,
  envelopes_enabled: true,
  categories: {
    payments: 'always_confirm',
    sends_publishes: 'always_confirm',
    deletes: 'always_confirm',
    account_changes: 'always_confirm',
  },
};

const rowStyle = {
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'space-between',
  gap: '16px',
  background: 'rgba(255,255,255,0.03)',
  border: '1px solid rgba(255,255,255,0.08)',
  borderRadius: '12px',
  padding: '16px',
};

const selectStyle = {
  background: 'rgba(0,0,0,0.4)',
  color: '#f0f0f5',
  border: '1px solid rgba(255,255,255,0.15)',
  borderRadius: '8px',
  padding: '8px 10px',
  fontSize: '13px',
  cursor: 'pointer',
};

export default function BrowserPolicyPanel({ wsRef: propWsRef }) {
  const socket = useAetherSocket();
  const wsRef = propWsRef || socket?.wsRef;
  const contextSendMessage = socket?.sendMessage;

  const [policy, setPolicy] = useState(PRESET);
  const [error, setError] = useState('');

  const sendEvent = useCallback(
    (type, payload = {}) => {
      const msg = {
        type,
        schema_version: 1,
        request_id: Date.now().toString(),
        payload,
      };
      if (contextSendMessage) {
        contextSendMessage(type, {
          schema_version: 1,
          request_id: msg.request_id,
          payload,
        });
      } else if (wsRef?.current?.readyState === 1 || wsRef?.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(msg));
      }
    },
    [contextSendMessage, wsRef]
  );

  useEffect(() => {
    sendEvent('BROWSER_POLICY_GET_REQUEST', {});
  }, [sendEvent]);

  useEffect(() => {
    const socketInstance = wsRef?.current;
    if (!socketInstance) return undefined;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'BROWSER_POLICY_GET_RESPONSE' && data.payload) {
          setPolicy({ ...PRESET, ...data.payload, categories: { ...PRESET.categories, ...(data.payload.categories || {}) } });
          setError('');
        } else if (data.type === 'BROWSER_POLICY_SET_RESPONSE' && data.payload) {
          // The response carries the full authoritative policy —
          // render exactly that (a rejected update echoes the
          // unchanged policy, so the controls snap back).
          if (data.payload.policy) {
            setPolicy({ ...PRESET, ...data.payload.policy, categories: { ...PRESET.categories, ...(data.payload.policy.categories || {}) } });
          }
          setError(data.payload.success ? '' : data.payload.error || 'The policy update was rejected.');
        }
      } catch {
        // A malformed frame is not a policy event; ignore it.
      }
    };
    socketInstance.addEventListener('message', handleMessage);
    return () => {
      socketInstance.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  const setToggle = (key, checked) => {
    setPolicy((prev) => ({ ...prev, [key]: checked }));
    sendEvent('BROWSER_POLICY_SET_REQUEST', { [key]: checked });
  };

  const setMode = (category, mode) => {
    setPolicy((prev) => ({ ...prev, categories: { ...prev.categories, [category]: mode } }));
    sendEvent('BROWSER_POLICY_SET_REQUEST', { categories: { [category]: mode } });
  };

  return (
    <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Browser Agent Policy</h2>
        <p style={{ color: '#9090a0', fontSize: '14px', margin: 0 }}>
          What the agent browser may do without stopping to ask. Browsing, reading, searching, and
          filling ordinary form fields are always free; the consequential categories below are
          governed by the mode you choose for each.
        </p>
      </div>

      {error && (
        <div
          style={{
            background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: '10px',
            padding: '12px 16px',
            color: '#fca5a5',
            fontSize: '13px',
          }}
        >
          {error}
        </div>
      )}

      <div style={rowStyle}>
        <div style={{ display: 'flex', gap: '12px' }}>
          <Globe size={20} color="#60a5fa" style={{ marginTop: '2px', flexShrink: 0 }} />
          <div>
            <div style={{ fontSize: '15px', fontWeight: 500 }}>Browser agent</div>
            <div style={{ color: '#9090a0', fontSize: '13px', marginTop: '4px', lineHeight: 1.5 }}>
              The master switch. Turning it off blocks new browser tasks outright; a task that is
              already running is not interrupted mid-action.
            </div>
          </div>
        </div>
        <input
          type="checkbox"
          data-testid="browser-policy-enabled"
          checked={!!policy.enabled}
          onChange={(e) => setToggle('enabled', e.target.checked)}
          style={{ width: '18px', height: '18px', cursor: 'pointer', flexShrink: 0 }}
        />
      </div>

      <div style={rowStyle}>
        <div style={{ display: 'flex', gap: '12px' }}>
          <Layers size={20} color="#a78bfa" style={{ marginTop: '2px', flexShrink: 0 }} />
          <div>
            <div style={{ fontSize: '15px', fontWeight: 500 }}>Per-task envelopes</div>
            <div style={{ color: '#9090a0', fontSize: '13px', marginTop: '4px', lineHeight: 1.5 }}>
              Allow the “for this task” grant on a confirmation card: a bounded pre-authorisation
              for one task, one category, one site. Envelopes never cover payments.
            </div>
          </div>
        </div>
        <input
          type="checkbox"
          data-testid="browser-policy-envelopes"
          checked={!!policy.envelopes_enabled}
          onChange={(e) => setToggle('envelopes_enabled', e.target.checked)}
          style={{ width: '18px', height: '18px', cursor: 'pointer', flexShrink: 0 }}
        />
      </div>

      <div style={rowStyle}>
        <div style={{ display: 'flex', gap: '12px' }}>
          <KeyRound size={20} color="#fbbf24" style={{ marginTop: '2px', flexShrink: 0 }} />
          <div>
            <div style={{ fontSize: '15px', fontWeight: 500 }}>Vault autofill</div>
            <div style={{ color: '#9090a0', fontSize: '13px', marginTop: '4px', lineHeight: 1.5 }}>
              Let the credential vault fill logins on their own sites. Entries marked
              ask-every-time still prompt before every fill.
            </div>
          </div>
        </div>
        <input
          type="checkbox"
          data-testid="browser-policy-vault-autofill"
          checked={!!policy.vault_autofill}
          onChange={(e) => setToggle('vault_autofill', e.target.checked)}
          style={{ width: '18px', height: '18px', cursor: 'pointer', flexShrink: 0 }}
        />
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
          <ShieldAlert size={18} color="#f87171" />
          <h3 style={{ fontSize: '16px', fontWeight: 500, margin: 0 }}>Consequential actions</h3>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {CATEGORIES.map((category) => (
            <div key={category.id} style={rowStyle}>
              <div>
                <div style={{ fontSize: '15px', fontWeight: 500 }}>{category.label}</div>
                <div style={{ color: '#9090a0', fontSize: '13px', marginTop: '4px', lineHeight: 1.5 }}>
                  {category.desc}
                </div>
                {category.note && (
                  <div style={{ color: '#fbbf24', fontSize: '12px', marginTop: '6px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Wallet size={13} />
                    {category.note}
                  </div>
                )}
              </div>
              <select
                data-testid={`browser-policy-mode-${category.id}`}
                value={policy.categories[category.id] || 'always_confirm'}
                onChange={(e) => setMode(category.id, e.target.value)}
                style={selectStyle}
              >
                {MODES.map((mode) => (
                  <option key={mode.value} value={mode.value}>
                    {mode.label}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
