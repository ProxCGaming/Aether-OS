import React from 'react';
import LoadStateRow from './LoadStateRow';

// Master build S19: Settings → Computer Use.
//
// Three stored fields, all riding the one CONFIG_UPDATE flow
// SettingsPanel owns (this panel is controlled, E6-F05 style
// like the Conversation/Calendar panels — it renders the
// applied values and reports edits; a refused write surfaces
// verbatim in configError and never lands):
//   computer_use_enabled       — the master switch, default OFF
//   computer_use_observe       — read the screen's accessibility
//                                text, default OFF
//   computer_use_vision_capture — off | ask | on_demand: the
//                                standing instruction for the
//                                screenshot fallback actuation
//                                (S20) may one day use. Stored
//                                and shown now, consumed then —
//                                this build takes NO screenshots
//                                in any mode, and the closed set
//                                deliberately has no "always on".
//
// The Control toggle is deliberately NOT a stored field:
// nothing in the engine can click or type yet, so a stored
// "control" value would claim a state that does not exist. It
// renders disabled as pure UI state with its arrival note, and
// becomes a real field only when actuation lands.
//
// Approvals are NOT a second system: actuation will inherit the
// tool approval mode from Settings → Tools, shown here
// read-only so the inheritance is visible.

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
  computerUseEnabled = false,
  onComputerUseEnabledChange,
  computerUseObserve = false,
  onComputerUseObserveChange,
  visionCapture = 'off',
  onVisionCaptureChange,
  approvalMode = 'ask',
}) {
  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Computer Use</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
          Lets Aether see what is on your screen so it can help with the app in front of you.
          This build can only LOOK — reading is all it does. Clicking, typing and other actions
          arrive with actuation in a later build, behind the same approval settings as every
          other tool.
        </p>
      </div>

      {!configAnswered && (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          loadingText="Loading your saved computer use settings…"
          offlineText="Waiting for the engine — your saved computer use settings will appear when it reconnects."
          testId="computer-use-config-load-state"
        />
      )}

      {configAnswered && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', background: 'var(--bg-base)', borderRadius: '12px', padding: '20px' }}>
          <ToggleRow
            testId="computer-use-enabled-toggle"
            title="Enable Computer Use"
            description="The master switch. Off means Aether has no desktop tools at all — the screen-reading tool is not even offered to the assistant."
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
          <ToggleRow
            testId="computer-use-control-toggle"
            title="Control — click, type and act (arrives with actuation (S20))"
            description="Not available in this build, so there is nothing to store yet. When control arrives it will use the approval mode below — there is no separate Computer Use approval system."
            checked={false}
            disabled
            onChange={null}
          />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '14px' }}>Screenshot fallback for later</div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px', lineHeight: 1.5 }}>
                Your standing instruction for the day control can fall back to a screenshot when
                the accessibility tree cannot describe a surface. Saved now, used then — this
                build takes no screenshots in any mode, and there is deliberately no
                &ldquo;always on&rdquo; choice: capture is never continuous.
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
              Computer Use inherits the tool approval mode from Settings → Tools — change it
              there. Screen reads themselves are read-only and never need approval.
            </div>
          </div>

          <p style={{ fontSize: '12px', color: 'var(--text-muted)', lineHeight: 1.5, margin: 0 }}>
            Privacy, exactly: observe reads accessibility text only, only the foreground
            window&rsquo;s tree, only when the assistant asks during a task you started. Nothing
            is captured continuously, nothing is stored beyond the turn&rsquo;s prompt, and no
            screenshots are taken in this build.
          </p>

          {configError ? (
            <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{configError}</div>
          ) : null}
        </div>
      )}
    </div>
  );
}
