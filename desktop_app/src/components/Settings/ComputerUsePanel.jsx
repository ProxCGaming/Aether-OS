import React, { useState, useEffect, useCallback } from 'react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';

// Master build S19 + S20: Settings → Computer Use.
//
// Stored fields ride the one CONFIG_UPDATE flow SettingsPanel
// owns (this panel is controlled, E6-F05 style — it renders the
// applied values and reports edits; a refused write surfaces
// verbatim in configError and never lands):
//   computer_use_enabled        — the master switch, default OFF
//   computer_use_observe        — read the screen's accessibility
//                                 text, default OFF (S19)
//   computer_use_vision_capture — off | ask | on_demand: when a
//                                 screenshot may be taken to ground
//                                 a control accessibility cannot
//                                 resolve. No "always on" exists.
//   computer_use_control        — actuation, default OFF (S20)
//   computer_use_control_ack    — the one-time first-enable
//                                 acknowledgement; control cannot
//                                 take effect without it (the
//                                 engine refuses the combination,
//                                 not just this panel)
//   computer_use_category_modes — per-category confirmation
//                                 modes (desktop/policy.py)
//   computer_use_app_rules      — the per-app allow/deny list
//   computer_use_envelopes_enabled / computer_use_pixel_fallback
//
// The Control toggle became a real stored field in S20, when
// actuation landed (owner decision O1); in S19 it rendered
// disabled as pure UI state because no actuation existed.
//
// Remembered grants are NOT config: they live in the engine's
// grant store and are listed/revoked over DESKTOP_GRANTS_* —
// this panel fetches them itself (the BrowserPolicyPanel
// pattern), because they change outside the config flow (the
// confirmation card creates them).
//
// Hard-confirm rule, shown where it binds: Close or quit and
// System actions ALWAYS confirm individually — their mode
// selects are disabled at Always confirm, envelopes and
// remembered grants never cover them, and the engine refuses
// to store a looser mode for them.

const CATEGORIES = [
  {
    id: 'observe',
    label: 'Observe — read the screen',
    desc: 'Reading the accessibility tree. Governed by the Observe switch above, not a mode.',
    locked: 'observe',
  },
  {
    id: 'open',
    label: 'Open & focus windows',
    desc: 'Bringing an app or window to the front.',
  },
  {
    id: 'interact',
    label: 'Interact — click & invoke',
    desc: 'Pressing buttons, choosing menu items, toggling controls (by accessibility first, pixels only under the pixel fallback).',
  },
  {
    id: 'type_into',
    label: 'Type into fields',
    desc: 'Setting a field\u2019s value or typing text. Password fields always ask, whatever mode is set here.',
  },
  {
    id: 'clipboard',
    label: 'Clipboard',
    desc: 'Reading the clipboard\u2019s text or replacing its contents.',
  },
  {
    id: 'close_or_quit',
    label: 'Close or quit',
    desc: 'Closing a window, quitting an app, Alt+F4.',
    locked: 'hard',
  },
  {
    id: 'system',
    label: 'System actions',
    desc: 'Shut down, restart, sign out, lock, and other operating-system actions.',
    locked: 'hard',
  },
];

const MODES = [
  { value: 'always_confirm', label: 'Always confirm' },
  { value: 'allow_with_envelope', label: 'Allow with envelope' },
  { value: 'allow_freely', label: 'Allow freely' },
];

const selectStyle = {
  background: 'var(--bg-base)',
  color: 'var(--text-primary)',
  border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-md)',
  padding: '8px 10px',
  fontSize: '13px',
  cursor: 'pointer',
};

const toggleStyle = {
  accentColor: 'var(--accent)',
  width: '18px',
  height: '18px',
  flexShrink: 0,
};

const buttonStyle = {
  background: 'var(--accent)',
  color: 'var(--bg-base)',
  border: 'none',
  borderRadius: 'var(--radius-md)',
  padding: '8px 14px',
  fontSize: '13px',
  cursor: 'pointer',
  flexShrink: 0,
};

const smallButtonStyle = {
  background: 'transparent',
  color: 'var(--text-secondary)',
  border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-md)',
  padding: '6px 10px',
  fontSize: '12px',
  cursor: 'pointer',
  flexShrink: 0,
};

function ToggleRow({ testId, title, description, checked, disabled, onChange }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
      <div>
        <div style={{ fontSize: '14px' }}>{title}</div>
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px', lineHeight: 1.5 }}>
          {description}
        </div>
      </div>
      <input
        type="checkbox"
        data-testid={testId}
        checked={checked}
        disabled={disabled}
        onChange={onChange ? (e) => onChange(e.target.checked) : undefined}
        readOnly={!onChange}
        style={toggleStyle}
      />
    </div>
  );
}

export default function ComputerUsePanel({
  configAnswered = true,
  isConnected = true,
  configError = '',
  configLoadError = '',
  onRetryConfigLoad,
  computerUseEnabled = false,
  onComputerUseEnabledChange,
  computerUseObserve = false,
  onComputerUseObserveChange,
  visionCapture = 'off',
  onVisionCaptureChange,
  computerUseControl = false,
  onComputerUseControlChange,
  computerUseControlAck = false,
  onComputerUseControlAckChange,
  computerUseCategoryModes = {},
  onComputerUseCategoryModeChange,
  computerUseAppRules = [],
  onComputerUseAppRulesChange,
  computerUseEnvelopesEnabled = true,
  onComputerUseEnvelopesEnabledChange,
  computerUsePixelFallback = false,
  onComputerUsePixelFallbackChange,
  approvalMode = 'ask',
  wsRef: propWsRef,
}) {
  const socket = useAetherSocket();
  const wsRef = propWsRef || socket?.wsRef;
  const contextSendMessage = socket?.sendMessage;

  const [grants, setGrants] = useState(null);
  const [grantsError, setGrantsError] = useState('');
  const [newApp, setNewApp] = useState('');
  const [newVerdict, setNewVerdict] = useState('deny');

  const sendEvent = useCallback(
    (type, payload = {}) => {
      const requestId = Date.now().toString();
      if (contextSendMessage) {
        contextSendMessage(type, { schema_version: 1, request_id: requestId, payload });
      } else if (wsRef?.current?.readyState === 1 || wsRef?.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type, schema_version: 1, request_id: requestId, payload }));
      }
    },
    [contextSendMessage, wsRef]
  );

  useEffect(() => {
    sendEvent('DESKTOP_GRANTS_REQUEST', { action: 'list' });
  }, [sendEvent]);

  useEffect(() => {
    const socketInstance = wsRef?.current;
    if (!socketInstance) return undefined;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'DESKTOP_GRANTS_RESPONSE' && data.payload) {
          if (data.payload.success === false || data.payload.error) {
            setGrantsError(data.payload.error || 'The remembered grants could not be loaded.');
          } else {
            setGrants(Array.isArray(data.payload.grants) ? data.payload.grants : []);
            setGrantsError('');
          }
        }
      } catch {
        // A malformed frame is not a grants event; ignore it.
      }
    };
    socketInstance.addEventListener('message', handleMessage);
    return () => {
      socketInstance.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  const revokeGrant = (grantId) => {
    sendEvent('DESKTOP_GRANTS_REQUEST', { action: 'revoke', grant_id: grantId });
  };

  const modeOf = (categoryId) => computerUseCategoryModes?.[categoryId] || 'always_confirm';

  const addAppRule = () => {
    const app = newApp.trim();
    if (!app || !onComputerUseAppRulesChange) return;
    const rest = (computerUseAppRules || []).filter(
      (rule) => (rule.app || '').toLowerCase() !== app.toLowerCase()
    );
    onComputerUseAppRulesChange([...rest, { app, verdict: newVerdict }]);
    setNewApp('');
  };

  const removeAppRule = (index) => {
    if (!onComputerUseAppRulesChange) return;
    onComputerUseAppRulesChange(
      (computerUseAppRules || []).filter((_, i) => i !== index)
    );
  };

  const setAppRuleVerdict = (index, verdict) => {
    if (!onComputerUseAppRulesChange) return;
    onComputerUseAppRulesChange(
      (computerUseAppRules || []).map((rule, i) => (i === index ? { ...rule, verdict } : rule))
    );
  };

  const controlReady = computerUseEnabled && computerUseControlAck;

  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Computer Use</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
          Lets Aether see what is on your screen — and, only with Control on below, act on it.
          Reading is accessibility text only. Acting goes through the confirmation policy on
          this page: every desktop action is classified, and closing or quitting apps and
          system actions always ask first, whatever you set.
        </p>
      </div>

      {!configAnswered && (
        <div>
          <LoadStateRow
            loadState={configLoadError ? 'error' : 'loading'}
            isConnected={isConnected}
            loadingText="Loading your saved computer use settings…"
            offlineText="Waiting for the engine — your saved computer use settings will appear when it reconnects."
            errorText={configLoadError || 'Your saved computer use settings could not be loaded.'}
            testId="computer-use-config-load-state"
          />
          {configLoadError && onRetryConfigLoad && (
            <button
              type="button"
              onClick={onRetryConfigLoad}
              style={{ marginTop: '8px', background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              Retry
            </button>
          )}
        </div>
      )}

      {configAnswered && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', background: 'var(--bg-base)', borderRadius: '12px', padding: '20px' }}>
          <ToggleRow
            testId="computer-use-enabled-toggle"
            title="Enable Computer Use"
            description="The master switch. Off means Aether has no desktop tools at all — neither the screen-reading tool nor the control tool is offered to the assistant."
            checked={computerUseEnabled}
            onChange={onComputerUseEnabledChange}
          />
          <ToggleRow
            testId="computer-use-observe-toggle"
            title="Observe — read the screen's accessibility text"
            description={
              computerUseEnabled
                ? 'When both switches are on, the assistant may ask for the current screen state during a task you started. What it receives is accessibility TEXT only — the names, roles and values Windows exposes for the foreground window\u2019s controls (a button\u2019s label, a text field\u2019s contents) — never a picture of your screen.'
                : 'Needs the master switch above first. When both are on, the assistant may ask for the current screen state during a task you started: accessibility TEXT only — the names, roles and values Windows exposes for the foreground window\u2019s controls — never a picture of your screen.'
            }
            checked={computerUseObserve}
            disabled={!computerUseEnabled}
            onChange={onComputerUseObserveChange}
          />

          {!computerUseControlAck && (
            <div
              data-testid="computer-use-ack-block"
              style={{ border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '10px' }}
            >
              <div style={{ fontSize: '14px' }}>Before Control can turn on — read this once</div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: 1.6 }}>
                Turning on Control means Aether will click and type in your apps — pressing
                buttons, filling fields, focusing and closing windows — when a task asks it
                to. Actions that close or quit apps, and system actions (shut down, restart,
                sign out), ALWAYS ask for your confirmation first, whatever the modes below
                say. You can pause desktop actions, take control back, or stop them for a
                task at any time from the chat window — stopping never kills the task itself.
              </div>
              <div>
                <button
                  type="button"
                  data-testid="computer-use-ack-button"
                  style={buttonStyle}
                  disabled={!computerUseEnabled}
                  onClick={() => onComputerUseControlAckChange && onComputerUseControlAckChange(true)}
                >
                  I understand — continue
                </button>
                {!computerUseEnabled && (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)', marginLeft: '10px' }}>
                    Turn on the master switch first.
                  </span>
                )}
              </div>
            </div>
          )}

          <ToggleRow
            testId="computer-use-control-toggle"
            title="Control — click, type and act"
            description={
              !computerUseEnabled
                ? 'Needs the master switch above first.'
                : !computerUseControlAck
                  ? 'Needs the one-time acknowledgement above first — control cannot be enabled without it.'
                  : 'When on, the assistant can act on your desktop during tasks you start, under the confirmation modes below. Turning this off stops actuation entirely; observe is unaffected.'
            }
            checked={computerUseControl}
            disabled={!controlReady}
            onChange={onComputerUseControlChange}
          />

          <ToggleRow
            testId="computer-use-pixel-fallback-toggle"
            title="Pixel fallback — raw mouse & keyboard"
            description="For controls the accessibility tree cannot reach, Aether can fall back to raw mouse clicks and keystrokes at screen coordinates. It always asks once per session before the first pixel action, and it is off unless you turn it on."
            checked={computerUsePixelFallback}
            disabled={!computerUseEnabled}
            onChange={onComputerUsePixelFallbackChange}
          />
          <ToggleRow
            testId="computer-use-envelopes-toggle"
            title="Envelopes — pre-approve a bounded run of actions"
            description="An envelope lets you approve, once, a bounded number of same-category actions in one app for the rest of a task (one hour at most). Envelopes never cover Close or quit or System actions."
            checked={computerUseEnvelopesEnabled}
            disabled={!computerUseEnabled}
            onChange={onComputerUseEnvelopesEnabledChange}
          />

          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ fontSize: '14px' }}>Confirmation modes, per kind of action</div>
            {CATEGORIES.map((category) => (
              <div
                key={category.id}
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}
              >
                <div>
                  <div style={{ fontSize: '13px' }}>{category.label}</div>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px', lineHeight: 1.5 }}>
                    {category.desc}
                    {category.locked === 'hard' && (
                      <> Locked to Always confirm: these never run under an envelope, a remembered grant, or Allow freely.</>
                    )}
                  </div>
                </div>
                {category.locked === 'observe' ? (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)', flexShrink: 0 }}>
                    Observe switch
                  </span>
                ) : (
                  <select
                    data-testid={`computer-use-category-${category.id}`}
                    value={category.locked === 'hard' ? 'always_confirm' : modeOf(category.id)}
                    disabled={category.locked === 'hard' || !computerUseEnabled}
                    onChange={(e) => onComputerUseCategoryModeChange
                      && onComputerUseCategoryModeChange(category.id, e.target.value)}
                    style={selectStyle}
                  >
                    {MODES.map((mode) => (
                      <option key={mode.value} value={mode.value}>{mode.label}</option>
                    ))}
                  </select>
                )}
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ fontSize: '14px' }}>Per-app rules</div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: 1.5 }}>
              Deny refuses every desktop action in that app outright — no prompt, and the
              refusal is logged. Apps are matched by the process name observe reports
              (for example <span style={{ fontFamily: 'monospace' }}>WINWORD.EXE</span>).
              Apps you do not list are allowed by default; the modes above still govern them.
            </div>
            {(computerUseAppRules || []).map((rule, index) => (
              <div
                key={`${rule.app}-${index}`}
                data-testid={`computer-use-app-rule-${index}`}
                style={{ display: 'flex', alignItems: 'center', gap: '10px' }}
              >
                <span style={{ fontFamily: 'monospace', fontSize: '13px', flex: 1 }}>{rule.app}</span>
                <select
                  data-testid={`computer-use-app-rule-verdict-${index}`}
                  value={rule.verdict}
                  onChange={(e) => setAppRuleVerdict(index, e.target.value)}
                  style={selectStyle}
                >
                  <option value="deny">Deny</option>
                  <option value="allow">Allow</option>
                </select>
                <button
                  type="button"
                  data-testid={`computer-use-app-rule-remove-${index}`}
                  style={smallButtonStyle}
                  onClick={() => removeAppRule(index)}
                >
                  Remove
                </button>
              </div>
            ))}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <input
                type="text"
                data-testid="computer-use-app-rule-app"
                placeholder="App process name, e.g. WINWORD.EXE"
                value={newApp}
                onChange={(e) => setNewApp(e.target.value)}
                style={{ ...selectStyle, flex: 1, cursor: 'text' }}
              />
              <select
                data-testid="computer-use-app-rule-new-verdict"
                value={newVerdict}
                onChange={(e) => setNewVerdict(e.target.value)}
                style={selectStyle}
              >
                <option value="deny">Deny</option>
                <option value="allow">Allow</option>
              </select>
              <button
                type="button"
                data-testid="computer-use-app-rule-add"
                style={smallButtonStyle}
                onClick={addAppRule}
              >
                Add rule
              </button>
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ fontSize: '14px' }}>Remembered grants</div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: 1.5 }}>
              When you tick &ldquo;remember&rdquo; on a confirmation card, that app + kind of
              action stops asking. Grants never exist for Close or quit or System actions.
              Revoking takes effect on the very next action.
            </div>
            {grantsError ? (
              <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{grantsError}</div>
            ) : grants === null ? (
              <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                Loading remembered grants…
              </div>
            ) : grants.length === 0 ? (
              <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                No remembered grants yet.
              </div>
            ) : (
              grants.map((grant) => (
                <div
                  key={grant.id}
                  data-testid={`computer-use-grant-${grant.id}`}
                  style={{ display: 'flex', alignItems: 'center', gap: '10px' }}
                >
                  <span style={{ fontSize: '13px', flex: 1 }}>
                    {grant.app} — {grant.category.replace(/_/g, ' ')}
                  </span>
                  <button
                    type="button"
                    data-testid={`computer-use-grant-revoke-${grant.id}`}
                    style={smallButtonStyle}
                    onClick={() => revokeGrant(grant.id)}
                  >
                    Revoke
                  </button>
                </div>
              ))
            )}
          </div>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '14px' }}>Screenshot grounding for UIA-blind controls</div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px', lineHeight: 1.5 }}>
                When a control cannot be resolved from accessibility text at all, control can
                fall back to one screenshot, sent to your vision model to locate the control;
                the screenshot is discarded as soon as the call returns — it is never saved,
                attached, or logged. There is deliberately no &ldquo;always on&rdquo; choice:
                capture is never continuous.
              </div>
            </div>
            <select
              data-testid="computer-use-vision-capture"
              value={visionCapture}
              onChange={(e) => onVisionCaptureChange && onVisionCaptureChange(e.target.value)}
              style={selectStyle}
            >
              <option value="off">Off — never take screenshots</option>
              <option value="ask">Ask — each time one is needed</option>
              <option value="on_demand">On demand — when accessibility text is not enough</option>
            </select>
          </div>

          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            Approvals:{' '}
            <span data-testid="computer-use-approval-mode">
              {approvalMode === 'write' ? 'Auto-approve writes' : 'Ask for approval'}
            </span>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px', lineHeight: 1.5 }}>
              Desktop actions also pass through the tool approval mode from Settings → Tools —
              the modes above decide what the Computer Use gate adds on top. Screen reads
              themselves are read-only and never need approval.
            </div>
          </div>

          <p style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: 1.5, margin: 0 }}>
            Privacy, exactly: observe reads accessibility text only, only the foreground
            window&rsquo;s tree, only when the assistant asks during a task you started.
            Nothing is captured continuously and observe results are not stored beyond the
            turn&rsquo;s prompt. Screenshots exist only inside a grounding call you allowed
            above, and are discarded when it returns.
          </p>

          {configError ? (
            <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{configError}</div>
          ) : null}
        </div>
      )}
    </div>
  );
}
