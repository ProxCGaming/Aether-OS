import React, { useEffect, useRef, useState } from 'react';
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
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  const [step, setStep] = useState(0);
  const [providerName, setProviderName] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [saveState, setSaveState] = useState(null); // null | 'saving' | 'error' | 'timeout'
  const [workspacePath, setWorkspacePath] = useState('');
  const [workspaceState, setWorkspaceState] = useState(null); // null | 'saving' | 'error' | 'timeout'
  const [workspaceError, setWorkspaceError] = useState('');
  const [workspaceSaved, setWorkspaceSaved] = useState(false);
  const [approvalMode, setApprovalMode] = useState('ask');
  const [approvalState, setApprovalState] = useState(null); // null | 'saving' | 'error' | 'timeout'

  // Lane C grouped S4: a save sent while CONNECTED to a wedged
  // engine sat at "Saving…" forever — the disconnected case had
  // its queued notice (E6-F03); the no-answer case had nothing, and
  // the step's button stayed disabled. Mirror ProviderSettings'
  // 35s rule (RUX-C-03): every save arms a timer, and if no
  // acknowledgement lands within 35s the state resolves to
  // 'timeout' — the no-answer note shows and the control works
  // again. A late acknowledgement still lands afterwards and
  // advances the step exactly as before.
  const SAVE_NO_ANSWER_MS = 35000;
  const saveTimersRef = useRef({});
  const clearSaveTimer = (kind) => {
    if (saveTimersRef.current[kind]) {
      clearTimeout(saveTimersRef.current[kind]);
      saveTimersRef.current[kind] = null;
    }
  };
  const armSaveTimer = (kind, setState) => {
    clearSaveTimer(kind);
    saveTimersRef.current[kind] = setTimeout(() => {
      setState((s) => (s === 'saving' ? 'timeout' : s));
    }, SAVE_NO_ANSWER_MS);
  };
  useEffect(() => () => {
    Object.values(saveTimersRef.current).forEach((t) => { if (t) clearTimeout(t); });
  }, []);

  useEffect(() => {
    if (!providerName && providers.length > 0) {
      setProviderName(providers[0].name);
    }
  }, [providers, providerName]);

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const payload = data.payload || {};
        if (data.type === 'PROVIDER_SAVE_RESPONSE') {
          clearSaveTimer('provider');
          if (payload.success) {
            setSaveState(null);
            // Advance only if the user is still on this step — a save
            // queued while offline can be acknowledged after they have
            // skipped ahead, and must not yank them back.
            setStep((s) => (s === 0 ? 1 : s));
          } else {
            setSaveState('error');
          }
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE') {
          clearSaveTimer('workspace');
          if (payload.success) {
            setWorkspaceState(null);
            setWorkspaceError('');
            setWorkspaceSaved(true);
            setStep((s) => (s === 1 ? 2 : s));
          } else {
            // A rejection is a real outcome: surface it and let the
            // step offer the picker again (see the step render).
            setWorkspaceState('error');
            setWorkspaceError(payload.error || '');
          }
        } else if (data.type === 'APPROVAL_MODE_SET_RESPONSE') {
          clearSaveTimer('approval');
          if (payload.success) {
            setApprovalState(null);
            setStep((s) => (s === 2 ? 3 : s));
          } else {
            setApprovalState('error');
          }
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
    armSaveTimer('provider', setSaveState);
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
      setWorkspaceSaved(false);
      setWorkspaceState('saving');
      armSaveTimer('workspace', setWorkspaceState);
      setWorkspaceError('');
      sendMessage('WORKSPACE_ADD_REQUEST', {
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { path: selected },
      });
    }
  };

  const saveApprovalMode = () => {
    if (approvalState === 'saving') return;
    setApprovalState('saving');
    armSaveTimer('approval', setApprovalState);
    sendMessage('APPROVAL_MODE_SET_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { mode: approvalMode },
    });
    // No optimistic advance: the APPROVAL_MODE_SET_RESPONSE handler
    // moves to the summary on success and surfaces a refusal here.
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
  const queuedStyle = { color: '#f59e0b', fontSize: '13px' };
  const errorStyle = { color: '#ef4444', fontSize: '13px' };

  // The summary may claim a full save only for choices the engine has
  // actually acknowledged. A choice whose save is still queued (sent
  // while offline, not yet answered) or was refused is named honestly
  // instead of being swept into "everything is saved".
  const pendingLabels = [];
  const failedLabels = [];
  if (saveState === 'saving') pendingLabels.push('your provider key');
  else if (saveState === 'error' || saveState === 'timeout') failedLabels.push('your provider key');
  if (workspaceState === 'saving') pendingLabels.push('your workspace folder');
  else if (workspaceState === 'error' || workspaceState === 'timeout') failedLabels.push('your workspace folder');
  if (approvalState === 'saving') pendingLabels.push('your approval default');
  else if (approvalState === 'error' || approvalState === 'timeout') failedLabels.push('your approval default');
  const joinLabels = (labels) => (labels.length <= 1
    ? labels[0] || ''
    : `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`);
  let summaryText;
  if (pendingLabels.length === 0 && failedLabels.length === 0) {
    summaryText = 'Everything you chose is saved in the engine and can be changed in Settings. Say hello, or hand Aether a task.';
  } else {
    const clauses = [];
    if (pendingLabels.length > 0) {
      clauses.push(`${joinLabels(pendingLabels)} ${pendingLabels.length > 1 ? 'are' : 'is'} queued and will be sent when the engine reconnects — not saved yet`);
    }
    if (failedLabels.length > 0) {
      clauses.push(`${joinLabels(failedLabels)} could not be saved`);
    }
    summaryText = `Everything else you chose is saved in the engine, but ${clauses.join('; ')}. You can change any of it in Settings. Say hello, or hand Aether a task.`;
  }

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

        {!isConnected && (
          <div style={{
            background: 'rgba(245,158,11,0.12)', border: '1px solid rgba(245,158,11,0.4)', color: '#fbbf24',
            borderRadius: '10px', padding: '10px 12px', fontSize: '13px', marginBottom: '16px',
          }}>
            You're not connected to the engine. Nothing is saved yet — saves are queued and sent when the connection returns, or skip setup and finish later in Settings.
          </div>
        )}

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
                <div style={errorStyle}>That key could not be saved — check it and try again, or skip this step.</div>
              )}
              {saveState === 'timeout' && (
                <div style={errorStyle}>No answer from the engine in 35s — the save may only be queued, or the engine may be busy or offline. Try again, or skip this step.</div>
              )}
              {saveState === 'saving' && !isConnected && (
                <div style={queuedStyle}>Queued — the engine is unreachable, so this key isn't saved yet. It will be sent when the connection returns, or skip this step.</div>
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
              <div style={{ fontFamily: 'monospace', fontSize: '13px', color: workspaceSaved ? '#10b981' : '#f0f0f5' }}>{workspacePath}</div>
            ) : null}
            {(!workspacePath || workspaceState === 'error' || workspaceState === 'timeout') && (
              <button style={{ ...primaryBtn, marginTop: workspacePath ? '10px' : 0 }} onClick={pickWorkspace}>Choose folder…</button>
            )}
            {workspaceState === 'saving' && isConnected && (
              <div style={{ color: '#9090a0', fontSize: '13px', marginTop: '10px' }}>Saving…</div>
            )}
            {workspaceState === 'saving' && !isConnected && (
              <div style={{ ...queuedStyle, marginTop: '10px' }}>
                Queued — the engine is unreachable, so this folder isn't saved yet. It will be sent when the connection returns, or skip this step.
              </div>
            )}
            {workspaceState === 'error' && (
              <div style={{ ...errorStyle, marginTop: '10px' }}>
                {workspaceError
                  ? `That folder could not be added: ${workspaceError}`
                  : 'That folder could not be added — try another, or skip this step.'}
              </div>
            )}
            {workspaceState === 'timeout' && (
              <div style={{ ...errorStyle, marginTop: '10px' }}>
                No answer from the engine in 35s — the save may only be queued, or the engine may be busy or offline. Choose the folder again to retry, or skip this step.
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '20px' }}>
              <button style={ghostBtn} onClick={onDone}>Skip setup</button>
              <div>
                <button style={ghostBtn} onClick={() => setStep(2)}>Skip step</button>
                <button style={primaryBtn} onClick={() => setStep(2)} disabled={!workspaceSaved}>Continue</button>
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
            {approvalState === 'error' && (
              <div style={{ ...errorStyle, marginTop: '10px' }}>That approval default could not be saved — try again, or skip setup.</div>
            )}
            {approvalState === 'timeout' && (
              <div style={{ ...errorStyle, marginTop: '10px' }}>No answer from the engine in 35s — the save may only be queued, or the engine may be busy or offline. Try again, or skip setup.</div>
            )}
            {approvalState === 'saving' && !isConnected && (
              <div style={{ ...queuedStyle, marginTop: '10px' }}>Queued — the engine is unreachable, so this choice isn't saved yet. It will be sent when the connection returns, or skip setup.</div>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '20px' }}>
              <button style={ghostBtn} onClick={onDone}>Skip setup</button>
              <button style={primaryBtn} onClick={saveApprovalMode} disabled={approvalState === 'saving'}>
                {approvalState === 'saving' ? 'Saving…' : 'Continue'}
              </button>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>You're set</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              {summaryText}
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
