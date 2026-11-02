import React, { useEffect, useState } from 'react';
import { useAetherSocket } from '../contexts/SocketContext';

/**
 * First-run onboarding (Epic 6, E6-02).
 *
 * Shown once, when no provider is configured and the engine's
 * onboarding_completed flag is false. Three skippable steps —
 * provider key, workspace folder, approval default — each wired to
 * the same contracts the Settings views use, so completing a step
 * here is identical to doing it in Settings. Finish or Skip both
 * call onDone, which persists the flag engine-side.
 */
export default function Onboarding({ providers = [], onDone }) {
  const { wsRef, sendMessage } = useAetherSocket();
  const [step, setStep] = useState(0);
  const [providerName, setProviderName] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [saveState, setSaveState] = useState(null); // null | 'saving' | 'error'
  const [workspacePath, setWorkspacePath] = useState('');
  const [approvalMode, setApprovalMode] = useState('ask');

  useEffect(() => {
    if (!providerName && providers.length > 0) {
      setProviderName(providers[0].name);
    }
  }, [providers, providerName]);

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PROVIDER_SAVE_RESPONSE') {
          if (data.payload.success) {
            setSaveState(null);
            setStep(1);
          } else {
            setSaveState('error');
          }
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE' && data.payload.success) {
          setStep(2);
        }
      } catch (e) {
        // ignore malformed frames
      }
    };
    if (wsRef.current) wsRef.current.addEventListener('message', handleMessage);
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  const saveProvider = () => {
    if (!providerName) return;
    setSaveState('saving');
    sendMessage('PROVIDER_SAVE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { provider: providerName, api_key: apiKey, base_url: '' },
    });
  };

  const pickWorkspace = async () => {
    if (!window.electronAPI?.showOpenDialog) return;
    const selected = await window.electronAPI.showOpenDialog({
      properties: ['openDirectory', 'createDirectory'],
      title: 'Choose your workspace folder',
    });
    if (selected) {
      setWorkspacePath(selected);
      sendMessage('WORKSPACE_ADD_REQUEST', {
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { path: selected },
      });
    }
  };

  const saveApprovalMode = () => {
    sendMessage('APPROVAL_MODE_SET_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { mode: approvalMode },
    });
    setStep(3);
  };

  const panelStyle = {
    background: 'rgba(20,20,28,0.97)', border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: '20px', padding: '32px', width: '440px', maxWidth: '92vw', color: '#f0f0f5',
  };
  const primaryBtn = {
    background: '#7c3aed', color: '#fff', border: 'none', padding: '10px 22px',
    borderRadius: '12px', cursor: 'pointer', fontWeight: 500,
  };
  const ghostBtn = {
    background: 'transparent', color: '#9090a0', border: 'none', padding: '10px 14px',
    cursor: 'pointer', fontSize: '13px',
  };
  const inputStyle = {
    padding: '12px 16px', color: '#fff', outline: 'none', width: '100%',
    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: '10px',
  };

  return (
    <div
      data-testid="onboarding-overlay"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(5,5,10,0.72)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
      }}
    >
      <div style={panelStyle}>
        <div style={{ fontSize: '12px', color: '#a78bfa', letterSpacing: '1px', textTransform: 'uppercase', marginBottom: '8px' }}>
          Welcome to Aether-OS · Step {Math.min(step + 1, 3)} of 3
        </div>

        {step === 0 && (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>Connect a model provider</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              Add an API key so Aether can think. Keys are stored in the engine's encrypted secret store — never in the app.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <select
                aria-label="Provider"
                value={providerName}
                onChange={(e) => setProviderName(e.target.value)}
                style={inputStyle}
              >
                {providers.map((p) => (
                  <option key={p.name} value={p.name} style={{ color: '#000' }}>
                    {p.display_name || p.name}
                  </option>
                ))}
              </select>
              <input
                type="password"
                aria-label="API key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="API key"
                style={inputStyle}
              />
              {saveState === 'error' && (
                <div style={{ color: '#ef4444', fontSize: '13px' }}>That key could not be saved — check it and try again, or skip this step.</div>
              )}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '20px' }}>
              <button style={ghostBtn} onClick={onDone}>Skip setup</button>
              <div>
                <button style={ghostBtn} onClick={() => setStep(1)}>Skip step</button>
                <button style={primaryBtn} onClick={saveProvider} disabled={saveState === 'saving' || !apiKey}>
                  {saveState === 'saving' ? 'Saving…' : 'Save & continue'}
                </button>
              </div>
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>Choose your workspace</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              The folder Aether works in by default. Tools stay inside your workspace roots unless you approve otherwise.
            </p>
            {workspacePath ? (
              <div style={{ fontFamily: 'monospace', fontSize: '13px', color: '#10b981' }}>{workspacePath}</div>
            ) : (
              <button style={primaryBtn} onClick={pickWorkspace}>Choose folder…</button>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '20px' }}>
              <button style={ghostBtn} onClick={onDone}>Skip setup</button>
              <div>
                <button style={ghostBtn} onClick={() => setStep(2)}>Skip step</button>
                <button style={primaryBtn} onClick={() => setStep(2)} disabled={!workspacePath}>Continue</button>
              </div>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>Approval default</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              How Aether handles tools that change files or run commands. You can change this any time in Settings.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <label style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', cursor: 'pointer' }}>
                <input type="radio" name="approval-mode" value="ask" checked={approvalMode === 'ask'} onChange={() => setApprovalMode('ask')} />
                <span><strong>Ask me</strong><br /><span style={{ color: '#9090a0', fontSize: '13px' }}>Every write or command needs your approval (recommended).</span></span>
              </label>
              <label style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', cursor: 'pointer' }}>
                <input type="radio" name="approval-mode" value="write" checked={approvalMode === 'write'} onChange={() => setApprovalMode('write')} />
                <span><strong>Write freely</strong><br /><span style={{ color: '#9090a0', fontSize: '13px' }}>File writes inside the workspace run without asking; risky actions still ask.</span></span>
              </label>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '20px' }}>
              <button style={ghostBtn} onClick={onDone}>Skip setup</button>
              <button style={primaryBtn} onClick={saveApprovalMode}>Continue</button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>You're set</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              Everything you chose is saved in the engine and can be changed in Settings. Say hello, or hand Aether a task.
            </p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '20px' }}>
              <button style={primaryBtn} onClick={onDone}>Start chatting</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
