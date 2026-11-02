import React, { useState, useEffect, useCallback } from 'react';
import { KeyRound, Pencil, Trash2, ShieldCheck } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

// Epic 8, E8-6.3: Settings → Password Vault. The owner's surface
// for the credential vault: entries are listed as METADATA ONLY —
// the engine never sends a password to this panel, so a password
// can be set or replaced here but never viewed. Fills are mediated
// by the engine (vault → form field, bound to the entry's site);
// whether agents may fill at all is the "Vault autofill" toggle in
// Browser Agent Policy, and entries marked ask-every-time prompt
// before every fill. Entries an agent saved during a signup task
// land here marked "Added by an agent" for review. The optional
// TOTP secret is for the owner's own manual use only — no agent
// path can read it.

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

const inputStyle = {
  background: 'rgba(0,0,0,0.4)',
  color: '#f0f0f5',
  border: '1px solid rgba(255,255,255,0.15)',
  borderRadius: '8px',
  padding: '9px 11px',
  fontSize: '13px',
  width: '100%',
  boxSizing: 'border-box',
};

const labelStyle = {
  display: 'block',
  fontSize: '12px',
  color: '#9090a0',
  marginBottom: '6px',
};

const badgeStyle = {
  display: 'inline-block',
  fontSize: '11px',
  padding: '2px 8px',
  borderRadius: '999px',
  border: '1px solid rgba(255,255,255,0.15)',
  color: '#c9c9d4',
  marginRight: '6px',
  marginTop: '6px',
};

const smallButtonStyle = {
  background: 'rgba(255,255,255,0.05)',
  color: '#f0f0f5',
  border: '1px solid rgba(255,255,255,0.12)',
  borderRadius: '8px',
  padding: '6px 10px',
  fontSize: '12px',
  cursor: 'pointer',
  display: 'inline-flex',
  alignItems: 'center',
  gap: '6px',
};

const EMPTY_FORM = { id: null, site: '', username: '', password: '', totp: '', ask: false };

export default function VaultPanel({ wsRef: propWsRef }) {
  const socket = useAetherSocket();
  const wsRef = propWsRef || socket?.wsRef;
  const contextSendMessage = socket?.sendMessage;

  const [entries, setEntries] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
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

  const requestList = useCallback(() => {
    sendEvent('VAULT_ENTRY_LIST_REQUEST', {});
  }, [sendEvent]);

  useEffect(() => {
    requestList();
  }, [requestList]);

  useEffect(() => {
    const socketInstance = wsRef?.current;
    if (!socketInstance) return undefined;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'VAULT_ENTRY_LIST_RESPONSE' && data.payload) {
          setEntries(Array.isArray(data.payload.entries) ? data.payload.entries : []);
          setError('');
        } else if (data.type === 'VAULT_ENTRY_SAVE_RESPONSE' && data.payload) {
          if (data.payload.success) {
            setForm(EMPTY_FORM);
            setError('');
            requestList();
          } else {
            setError(data.payload.error || 'The entry could not be saved.');
          }
        } else if (data.type === 'VAULT_ENTRY_DELETE_RESPONSE' && data.payload) {
          if (data.payload.success) {
            requestList();
          } else {
            setError('That entry could not be deleted.');
          }
        }
      } catch {
        // A malformed frame is not a vault event; ignore it.
      }
    };
    socketInstance.addEventListener('message', handleMessage);
    return () => {
      socketInstance.removeEventListener('message', handleMessage);
    };
  }, [wsRef, requestList]);

  const saveForm = () => {
    sendEvent('VAULT_ENTRY_SAVE_REQUEST', {
      id: form.id,
      site: form.site,
      username: form.username,
      password: form.password,
      ask_every_time: form.ask,
      totp_secret: form.totp,
    });
  };

  const startEdit = (entry) => {
    // Passwords are never sent to this panel, so the password (and
    // TOTP) fields start blank: blank means "keep the stored one".
    setForm({
      id: entry.id,
      site: entry.site,
      username: entry.username,
      password: '',
      totp: '',
      ask: !!entry.ask_every_time,
    });
    setError('');
  };

  return (
    <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Password Vault</h2>
        <p style={{ color: '#9090a0', fontSize: '14px', margin: 0, lineHeight: 1.5 }}>
          Logins the agent browser may fill for you, each bound to its own site. A saved password
          travels from the vault straight into the site&apos;s login form — it is never shown to
          the agent, and it can be replaced here but never viewed. Whether agents may fill at all
          is controlled by the Vault autofill toggle under Browser Agent.
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

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {entries.length === 0 && (
          <div style={{ color: '#9090a0', fontSize: '13px' }}>
            No credentials saved yet. Add one below, or let an agent save a login it creates
            during a signup task — it will appear here for your review.
          </div>
        )}
        {entries.map((entry) => (
          <div key={entry.id} data-testid={`vault-entry-${entry.id}`} style={rowStyle}>
            <div style={{ display: 'flex', gap: '12px' }}>
              <KeyRound size={20} color="#fbbf24" style={{ marginTop: '2px', flexShrink: 0 }} />
              <div>
                <div style={{ fontSize: '15px', fontWeight: 500 }}>
                  {entry.site}
                  <span style={{ color: '#9090a0', fontWeight: 400 }}> — {entry.username}</span>
                </div>
                <div>
                  {entry.ask_every_time && <span style={badgeStyle}>Ask every time</span>}
                  {entry.created_by === 'agent' ? (
                    <span style={{ ...badgeStyle, borderColor: 'rgba(251,191,36,0.4)', color: '#fbbf24' }}>
                      Added by an agent — review it
                    </span>
                  ) : (
                    <span style={badgeStyle}>Added by you</span>
                  )}
                  {entry.last_used_at ? (
                    <span style={badgeStyle}>
                      Last used {new Date(entry.last_used_at * 1000).toLocaleDateString()}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
              <button
                type="button"
                data-testid={`vault-edit-${entry.id}`}
                style={smallButtonStyle}
                onClick={() => startEdit(entry)}
              >
                <Pencil size={13} /> Edit
              </button>
              <button
                type="button"
                data-testid={`vault-delete-${entry.id}`}
                style={smallButtonStyle}
                onClick={() => sendEvent('VAULT_ENTRY_DELETE_REQUEST', { id: entry.id })}
              >
                <Trash2 size={13} /> Delete
              </button>
            </div>
          </div>
        ))}
      </div>

      <div style={{ ...rowStyle, display: 'block' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '14px' }}>
          <ShieldCheck size={18} color="#34d399" />
          <h3 style={{ fontSize: '16px', fontWeight: 500, margin: 0 }}>
            {form.id ? 'Edit credential' : 'Add a credential'}
          </h3>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
          <div>
            <label style={labelStyle} htmlFor="vault-site">Site</label>
            <input
              id="vault-site"
              data-testid="vault-site-input"
              style={inputStyle}
              placeholder="github.com"
              value={form.site}
              onChange={(e) => setForm((f) => ({ ...f, site: e.target.value }))}
            />
          </div>
          <div>
            <label style={labelStyle} htmlFor="vault-username">Username</label>
            <input
              id="vault-username"
              data-testid="vault-username-input"
              style={inputStyle}
              placeholder="you@example.com"
              value={form.username}
              onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
            />
          </div>
          <div>
            <label style={labelStyle} htmlFor="vault-password">Password</label>
            <input
              id="vault-password"
              data-testid="vault-password-input"
              style={inputStyle}
              type="password"
              placeholder={form.id ? 'Leave blank to keep the current one' : 'The login password'}
              value={form.password}
              onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
            />
          </div>
          <div>
            <label style={labelStyle} htmlFor="vault-totp">TOTP secret (optional)</label>
            <input
              id="vault-totp"
              data-testid="vault-totp-input"
              style={inputStyle}
              type="password"
              placeholder="For your own manual use only"
              value={form.totp}
              onChange={(e) => setForm((f) => ({ ...f, totp: e.target.value }))}
            />
          </div>
        </div>
        <p style={{ color: '#9090a0', fontSize: '12px', lineHeight: 1.5, margin: '12px 0 0' }}>
          The TOTP secret is stored encrypted for your own manual use only — agents can never
          read it, and the agent never enters 2FA codes.
        </p>
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            marginTop: '14px',
            fontSize: '13px',
            cursor: 'pointer',
          }}
        >
          <input
            type="checkbox"
            data-testid="vault-ask-checkbox"
            checked={form.ask}
            onChange={(e) => setForm((f) => ({ ...f, ask: e.target.checked }))}
            style={{ width: '16px', height: '16px', cursor: 'pointer' }}
          />
          Ask every time before filling this credential
        </label>
        <div style={{ display: 'flex', gap: '10px', marginTop: '16px' }}>
          <button
            type="button"
            data-testid="vault-save-button"
            onClick={saveForm}
            style={{
              background: '#fbbf24',
              color: '#1a1a22',
              border: 'none',
              borderRadius: '8px',
              padding: '9px 18px',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            {form.id ? 'Save changes' : 'Add credential'}
          </button>
          {form.id && (
            <button
              type="button"
              data-testid="vault-cancel-button"
              onClick={() => setForm(EMPTY_FORM)}
              style={smallButtonStyle}
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
