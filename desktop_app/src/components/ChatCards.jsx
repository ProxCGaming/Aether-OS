import React, { useState, useEffect, useRef } from 'react';
import { X, AlertCircle, ShieldAlert, TerminalSquare, ChevronDown, CheckCircle2, Copy, CheckCheck, ArrowRight, ArrowDownRight, ArrowUpRight } from 'lucide-react';
import { parseApprovalEditedArgs, browserEnvelopeGrant, desktopGrantSpec } from './approvalLogic';

// Wave 5 (Annex C row 6): the transcript-rendering cards —
// payload drawers, the tool / node activity cards, the thinking
// accordion and the approval surfaces they share — extracted
// from DraggableChatWindow so the message-list rendering (see
// ChatTranscript) depends on them directly instead of through
// the window component. DraggableChatWindow re-exports the
// public names, so existing imports from it keep working.

export function getNodeStyle(nodeName = '') {
  const n = String(nodeName).toLowerCase();
  if (n.includes('supervisor')) {
    return { color: 'var(--accent)', bg: 'rgba(var(--accent-rgb), 0.16)', border: 'rgba(var(--accent-rgb), 0.35)', name: 'SUPERVISOR' };
  }
  if (n.includes('plan')) {
    return { color: '#93c5fd', bg: 'rgba(96, 165, 250, 0.16)', border: 'rgba(96, 165, 250, 0.35)', name: 'PLANNER' };
  }
  if (n.includes('code')) {
    return { color: '#6ee7b7', bg: 'rgba(52, 211, 153, 0.16)', border: 'rgba(52, 211, 153, 0.35)', name: 'CODER' };
  }
  if (n.includes('research')) {
    return { color: '#7dd3fc', bg: 'rgba(56, 189, 248, 0.16)', border: 'rgba(56, 189, 248, 0.35)', name: 'RESEARCHER' };
  }
  if (n.includes('tool') || n === 'execute_tool') {
    return { color: '#fde047', bg: 'rgba(251, 191, 36, 0.16)', border: 'rgba(251, 191, 36, 0.35)', name: 'EXECUTE_TOOL' };
  }
  if (n.includes('user')) {
    return { color: '#f472b6', bg: 'rgba(244, 114, 182, 0.16)', border: 'rgba(244, 114, 182, 0.35)', name: 'USER' };
  }
  return { color: '#e2e8f0', bg: 'rgba(255, 255, 255, 0.08)', border: 'rgba(255, 255, 255, 0.15)', name: n.toUpperCase() || 'NODE' };
}

export function useSmoothScroll(ref, active = true) {
  useEffect(() => {
    const el = ref.current;
    if (!el || !active) return;

    let targetScroll = el.scrollTop;
    let isScrolling = false;
    let frameId;

    const updateScroll = () => {
      if (!el) return;
      
      // Interpolate towards target using lerp (0.15 factor matching Dropdown)
      el.scrollTop += (targetScroll - el.scrollTop) * 0.15;
      
      if (Math.abs(targetScroll - el.scrollTop) > 0.5) {
        frameId = requestAnimationFrame(updateScroll);
      } else {
        el.scrollTop = targetScroll;
        isScrolling = false;
      }
    };

    const onWheel = (e) => {
      const maxScroll = el.scrollHeight - el.clientHeight;
      if (maxScroll <= 0) return;

      const goingDown = e.deltaY > 0;
      const goingUp = e.deltaY < 0;
      const atTop = el.scrollTop <= 0;
      const atBottom = el.scrollTop >= maxScroll - 1;

      if ((goingDown && !atBottom) || (goingUp && !atTop)) {
        e.preventDefault();
        targetScroll = Math.max(0, Math.min(maxScroll, targetScroll + e.deltaY));
        
        if (!isScrolling) {
          isScrolling = true;
          frameId = requestAnimationFrame(updateScroll);
        }
      }
    };

    const onScroll = () => {
      if (!isScrolling) {
        targetScroll = el.scrollTop;
      }
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('scroll', onScroll, { passive: true });
    
    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('scroll', onScroll);
      if (frameId) cancelAnimationFrame(frameId);
    };
  }, [ref, active]);
}

export function PayloadDrawer({ title, data, icon: Icon, defaultOpen = false, emptyText = 'No payload' }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [copied, setCopied] = useState(false);
  const bodyRef = useRef(null);

  useSmoothScroll(bodyRef, isOpen);

  const isNoneOrEmpty = data === undefined || data === null || data === '' || (typeof data === 'object' && Object.keys(data).length === 0);

  let formattedText = '';
  if (data === undefined || data === null) {
    formattedText = emptyText;
  } else if (typeof data === 'string') {
    formattedText = data;
  } else {
    try {
      formattedText = JSON.stringify(data, null, 2);
    } catch (e) {
      formattedText = String(data);
    }
  }

  // Summary badge count
  let countLabel = '';
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    const keys = Object.keys(data);
    countLabel = `${keys.length} ${keys.length === 1 ? 'param' : 'params'}`;
  } else if (Array.isArray(data)) {
    countLabel = `${data.length} ${data.length === 1 ? 'item' : 'items'}`;
  } else if (typeof data === 'string' && data.length > 0) {
    countLabel = `${data.length} chars`;
  }

  const handleCopy = async (e) => {
    e.stopPropagation();
    try {
      // D1-C8: writeText is async — the old synchronous try/catch
      // could not see its rejection and showed "Copied" for a copy
      // that never happened. Await it; only then claim success.
      await navigator.clipboard.writeText(formattedText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch (err) {}
  };

  return (
    <div className="payload-drawer-container">
      <div 
        className={`payload-drawer-header ${isOpen ? 'open' : ''}`}
        onClick={() => setIsOpen(!isOpen)}
        title="Click to toggle payload drawer"
      >
        <div className="payload-title-group">
          {Icon ? <Icon size={12} className="payload-icon" /> : <TerminalSquare size={12} className="payload-icon" />}
          <span className="payload-title">{title}</span>
          {countLabel && <span className="payload-count-badge">{countLabel}</span>}
        </div>
        <div className="payload-actions-group">
          <button 
            type="button" 
            className="payload-copy-btn" 
            onClick={handleCopy} 
            title="Copy payload to clipboard"
          >
            {copied ? <CheckCheck size={11} color="#4CC38A" /> : <Copy size={11} />}
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </button>
          <ChevronDown size={13} className={`payload-chevron ${isOpen ? 'rotated' : ''}`} />
        </div>
      </div>
      {isOpen && (
        <div ref={bodyRef} className="payload-drawer-body chat-scroll selectable-text">
          {isNoneOrEmpty && formattedText === '{}' ? (
            <div className="payload-empty-notice">&#123;&#125; (No parameters required)</div>
          ) : (
            <pre className="payload-code-block">{formattedText}</pre>
          )}
        </div>
      )}
    </div>
  );
}

// Epic 2 UI batch: the engine may attach a preview to an approval
// request — a unified diff (edit_file), or an error explaining why no
// preview could be built. Rendered inside both approval surfaces
// (the ToolActivityCard box and the standalone approval card) so the
// user sees what they are approving before deciding.
export function ApprovalPreview({ preview }) {
  if (!preview) return null;
  if (typeof preview.diff === 'string' && preview.diff.length > 0) {
    const diffLines = preview.diff.split('\n');
    return (
      <div
        className="approval-diff-preview"
        data-testid="approval-diff-preview"
        style={{
          background: 'var(--bg-base)',
          border: '1px solid var(--border-default)',
          borderRadius: '8px',
          padding: '10px 12px',
          maxHeight: '240px',
          overflowY: 'auto',
          fontFamily: 'var(--font-mono)',
          fontSize: '12px',
          lineHeight: 1.5,
        }}
      >
        {diffLines.map((line, idx) => {
          let cls = 'diff-line diff-line-context';
          let color = 'var(--text-secondary)';
          let background = 'transparent';
          if (line.startsWith('+++') || line.startsWith('---')) {
            cls = 'diff-line diff-line-meta';
            color = 'var(--text-primary)';
          } else if (line.startsWith('+')) {
            cls = 'diff-line diff-line-add';
            color = 'var(--success)';
            background = 'rgba(76, 195, 138, 0.08)';
          } else if (line.startsWith('-')) {
            cls = 'diff-line diff-line-del';
            color = 'var(--danger)';
            background = 'rgba(201, 42, 42, 0.08)';
          } else if (line.startsWith('@@')) {
            cls = 'diff-line diff-line-hunk';
            color = 'var(--text-muted)';
          }
          return (
            <div key={idx} className={cls} style={{ color, background, whiteSpace: 'pre', padding: '0 4px', borderRadius: '3px' }}>
              {line || ' '}
            </div>
          );
        })}
      </div>
    );
  }
  if (typeof preview.error === 'string' && preview.error.length > 0) {
    return (
      <div
        className="approval-preview-error"
        data-testid="approval-preview-error"
        style={{
          fontSize: '12px',
          color: 'var(--accent)',
          background: 'rgba(var(--accent-rgb), 0.1)',
          border: '1px solid rgba(var(--accent-rgb), 0.3)',
          borderRadius: '8px',
          padding: '8px 10px',
        }}
      >
        ⚠ No preview available: {preview.error}
      </div>
    );
  }
  return null;
}

// Epic 2, E2-09 (scoped approvals): when the pending call targets a
// file path, the approval can be remembered for that file or its
// folder. Returns the path string, or null when scoping doesn't apply.
export function approvalScopePath(args) {
  if (args && typeof args.path === 'string' && args.path.length > 0) return args.path;
  return null;
}
// Epic 8, E8-1.1a: map a message to the rewind point its step menu
// shows. The manifest holds points for the session's LAST task only
// (grill Q5), and the rendered transcript does not carry task ids —
// the current task is therefore identified structurally: it starts
// at the last user message that is not a steering message (steering
// continues the running task rather than starting one). The task's
// opening message maps to point 0 ("Before this task began"); every
// later message in the task maps to the latest recorded point (the
// manifest is a per-step trail, not a per-message one). Messages
// before the current task begins are 'older': no point is offered
// for them. Returns null only when there is no manifest at all.
export function getRewindPointForMessage(messages, index, manifest) {
  if (!manifest) return null;
  const entries = Array.isArray(manifest.entries) ? manifest.entries : [];
  let taskStart = -1;
  (messages || []).forEach((m, j) => {
    if (m && m.role === 'user' && !m.steering) taskStart = j;
  });
  if (taskStart === -1 || index < taskStart) return { scope: 'older', entry: null };
  if (index === taskStart) return { scope: 'current', entry: entries[0] || null };
  return { scope: 'current', entry: entries[entries.length - 1] || null };
}

export function BrowserConfirmationControl({ confirmation, grantChecked = false, onGrantCheckedChange = null }) {
  if (!confirmation) return null;
  return (
    <>
      <div
        data-testid="browser-confirmation-description"
        style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '12px', borderRadius: '8px', fontSize: '14px', color: 'var(--text-primary)' }}
      >
        {confirmation.description}
      </div>
      {confirmation.category === 'payments' ? (
        <div style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
          Payments are always confirmed individually — no envelope can cover them.
        </div>
      ) : (
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: 'var(--text-primary)', marginTop: '10px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            data-testid="browser-envelope-checkbox"
            checked={!!grantChecked}
            onChange={(e) => { if (onGrantCheckedChange) onGrantCheckedChange(e.target.checked); }}
          />
          Allow {confirmation.category.replace(/_/g, ' ')} actions on {confirmation.site_name || confirmation.site_host} for the rest of this task
        </label>
      )}
    </>
  );
}


// Master build S20: the desktop confirmation control — the
// browser control's analogue for Computer Use actuation. The
// description names the exact action (the resolved element as
// the OS states it). The grant opt-ins follow the engine's
// rules, stated on the card: the hard-confirm categories
// (close_or_quit, system) and password-field targets NEVER
// offer envelope/remember boxes — they always ask individually.
// A pixel action's approval IS the session's pixel consent, and
// a vision action's approval covers the one screenshot — both
// notes say so where the decision is made.
export function DesktopConfirmationControl({ confirmation, choice = {}, onChoiceChange = null }) {
  if (!confirmation) return null;
  const categoryLabel = (confirmation.category || '').replace(/_/g, ' ');
  const appSuffix = confirmation.app ? ` in ${confirmation.app}` : '';
  return (
    <>
      <div
        data-testid="desktop-confirmation-description"
        style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '12px', borderRadius: '8px', fontSize: '14px', color: 'var(--text-primary)' }}
      >
        {confirmation.description}
      </div>
      {confirmation.hard_confirm ? (
        <div style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
          This kind of action always asks individually — no envelope or remembered grant can cover it.
        </div>
      ) : confirmation.force_confirm ? (
        <div style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
          This target is a password field — it always asks individually.
        </div>
      ) : (
        <>
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: 'var(--text-primary)', marginTop: '10px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              data-testid="desktop-envelope-checkbox"
              checked={!!choice.envelope}
              onChange={(e) => { if (onChoiceChange) onChoiceChange('envelope', e.target.checked); }}
            />
            Allow {categoryLabel} actions{appSuffix} for the rest of this task
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: 'var(--text-primary)', marginTop: '6px', cursor: 'pointer' }}>
            <input
              type="checkbox"
              data-testid="desktop-remember-checkbox"
              checked={!!choice.remember}
              onChange={(e) => { if (onChoiceChange) onChoiceChange('remember', e.target.checked); }}
            />
            Remember: allow {categoryLabel} actions{appSuffix} without asking (revocable in Settings → Computer Use)
          </label>
        </>
      )}
      {confirmation.via === 'pixel' && (
        <div data-testid="desktop-pixel-note" style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '8px' }}>
          Approving also allows pixel (mouse/keyboard) input for the rest of this session.
        </div>
      )}
      {confirmation.via === 'vision' && (
        <div data-testid="desktop-vision-note" style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '8px' }}>
          Approving takes one screenshot to locate the control; the screenshot is discarded as soon as the call returns.
        </div>
      )}
    </>
  );
}


export function ToolActivityCard({ payload, isNested = false, pendingApproval = null, onApproveTool = null, onOpenArtifact = null, envelopeGrantChecked = false, onEnvelopeGrantCheckedChange = null, desktopGrantChoice = {}, onDesktopGrantChoiceChange = null }) {
  // Epic 8, E8-1.3: approval argument editor state (hooks first —
  // the early return below must stay after them).
  const [argsEditorOpen, setArgsEditorOpen] = useState(false);
  const [argsText, setArgsText] = useState('');
  const [argsError, setArgsError] = useState('');

  if (!payload) return null;

  const { tool_name, tool_args, status, result, node, from_node, to_node, edited } = payload;

  const isPending = status === 'pending';
  const isFailed = status === 'failed';
  const needsApproval = isPending && !!pendingApproval;
  const approvalPreview = (pendingApproval && pendingApproval.preview) || payload.preview || null;
  const scopePath = approvalScopePath((pendingApproval && pendingApproval.tool_args) || tool_args);

  const callerNode = from_node || node || 'agent';
  // Epic 8, E8-1.3: approving from the drawer goes through the
  // argument editor when it is open. An unchanged editor (or one
  // never opened) calls back with the exact legacy argument list;
  // a real edit appends editedArgs as the final argument.
  const approvalArgs = (pendingApproval && pendingApproval.tool_args) || tool_args || {};
  // Wave 2 slice 2 (RUX-A-05): a decision that could only be queued
  // (engine unreachable) disables the card — the decision is final
  // and will flush on reconnect; App marks the approval message for us.
  const decisionQueued = !!(pendingApproval && pendingApproval.decisionQueued);
  const browserConfirmation = (pendingApproval && pendingApproval.browser_confirmation) || payload.browser_confirmation || null;
  const desktopConfirmation = (pendingApproval && pendingApproval.desktop_confirmation) || payload.desktop_confirmation || null;
  const approveWithEdits = (scope) => {
    if (!onApproveTool || !pendingApproval) return;
    // N11: the envelope grant rides the same sixth-argument
    // contract as the standalone approval card, via the shared
    // browserEnvelopeGrant helper — one payload, both surfaces.
    // S20: the desktop grant spec rides as the SEVENTH argument
    // under the same discipline (approvalLogic.desktopGrantSpec);
    // when neither grant exists the legacy call shapes are
    // preserved exactly.
    const envelope = browserEnvelopeGrant(browserConfirmation, envelopeGrantChecked);
    const desktopSpec = desktopGrantSpec(desktopConfirmation, desktopGrantChoice);
    const approve = (s, edited) => {
      if (desktopSpec) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, s, edited, envelope, desktopSpec);
      else if (envelope) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, s, edited, envelope);
      else if (edited) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, s, edited);
      else if (s) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, s);
      else onApproveTool(pendingApproval.id, true, pendingApproval.approval_key);
    };
    if (!argsEditorOpen) {
      approve(scope, undefined);
      return;
    }
    const { editedArgs, error } = parseApprovalEditedArgs(argsText, approvalArgs);
    if (error) {
      setArgsError(error);
      return;
    }
    approve(scope, editedArgs || undefined);
  };
  const openArgsEditor = () => {
    setArgsText(JSON.stringify(approvalArgs, null, 2));
    setArgsError('');
    setArgsEditorOpen(true);
  };

  const targetNode = to_node || 'execute_tool';

  return (
    <div className={`tool-activity-card ${isNested ? 'nested' : ''} ${isPending ? 'pending' : ''} ${isFailed ? 'failed' : ''} ${needsApproval ? 'approval-required' : ''}`}>
      <div className="tool-card-topbar">
        <div className="tool-identity">
          <TerminalSquare size={14} className="tool-terminal-icon" />
          <span className="tool-call-name">{tool_name || 'unknown_tool'}</span>
          {tool_args?.path && (
            <button
              type="button"
              className="tool-card-file-link"
              data-testid="tool-card-file-link"
              onClick={(e) => {
                e.stopPropagation();
                if (onOpenArtifact) onOpenArtifact(tool_args.path);
              }}
              title={`View in artifacts: ${tool_args.path}`}
            >
              {tool_args.path.split(/[\/]/).pop()}
            </button>
          )}
          <span className="tool-route-badge">
            {callerNode.toUpperCase()} ➔ {targetNode.toUpperCase()}
          </span>
        </div>
        {edited && (
          <span data-testid="tool-edited-badge" style={{ fontSize: '10px', fontWeight: 600, color: 'var(--accent)', background: 'rgba(var(--accent-rgb), 0.15)', border: '1px solid rgba(var(--accent-rgb), 0.4)', borderRadius: '999px', padding: '2px 8px', marginRight: '8px' }}>edited by you</span>
        )}
        <div className={`tool-status-pill tool-status-${needsApproval ? 'approval' : (status || 'pending')}`}>
          {needsApproval ? <ShieldAlert size={11} color="#F5B942" /> : isPending ? <span className="tool-spinner-dot" /> : isFailed ? <AlertCircle size={11} /> : <CheckCircle2 size={11} />}
          <span>{needsApproval ? 'approval required' : (status || 'pending')}</span>
        </div>
      </div>

      <div className="tool-payloads-stack">
        <PayloadDrawer 
          title="📥 Request Payload" 
          data={tool_args || {}} 
          icon={ArrowDownRight}
          defaultOpen={false}
          emptyText="{}"
        />
        
        {needsApproval ? (
          <div style={{
            padding: '14px 16px',
            background: 'rgba(var(--accent-rgb), 0.08)',
            border: '1px solid rgba(var(--accent-rgb), 0.35)',
            borderRadius: '10px',
            marginTop: '8px',
            display: 'flex',
            flexDirection: 'column',
            gap: '10px'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--accent)', fontSize: '13px', fontWeight: 600 }}>
              <ShieldAlert size={16} color="#F5B942" />
              <span>Execution Approval Required</span>
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              The agent is requesting permission to execute <strong>{tool_name}</strong>.
            </div>
            <ApprovalPreview preview={approvalPreview} />
            {browserConfirmation && (
              <BrowserConfirmationControl
                confirmation={browserConfirmation}
                grantChecked={envelopeGrantChecked}
                onGrantCheckedChange={onEnvelopeGrantCheckedChange}
              />
            )}
            {desktopConfirmation && (
              <DesktopConfirmationControl
                confirmation={desktopConfirmation}
                choice={desktopGrantChoice}
                onChoiceChange={onDesktopGrantChoiceChange}
              />
            )}
            {!argsEditorOpen ? (
              <button
                data-testid="approval-edit-args-btn"
                onClick={openArgsEditor}
                disabled={decisionQueued}
                style={{ alignSelf: 'flex-start', padding: '6px 12px', borderRadius: '8px', background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}>
                Edit arguments
              </button>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <textarea
                  data-testid="approval-args-editor"
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  rows={6}
                  spellCheck={false}
                  style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', color: 'var(--text-primary)', border: '1px solid var(--border-default)', borderRadius: '8px', padding: '10px', fontFamily: 'var(--font-mono)', fontSize: '12px' }}
                />
                {argsError && (
                  <div data-testid="approval-args-error" style={{ fontSize: '12px', color: 'var(--danger)' }}>{argsError}</div>
                )}
              </div>
            )}
            {decisionQueued && (
              <div data-testid="approval-decision-queued" style={{ padding: '8px 12px', borderRadius: '8px', background: 'rgba(var(--accent-rgb), 0.15)', border: '1px solid rgba(var(--accent-rgb), 0.35)', color: 'var(--accent)', fontSize: '12px' }}>
                {pendingApproval.decisionApproved ? 'Approval' : 'Rejection'} queued — will send when the engine reconnects.
              </div>
            )}
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'flex-end', marginTop: '4px' }}>
              <button 
                onClick={() => onApproveTool && onApproveTool(pendingApproval.id, false, pendingApproval.approval_key)}
                disabled={decisionQueued}
                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  background: 'var(--bg-hover)',
                  color: 'var(--text-primary)',
                  border: '1px solid var(--border-strong)',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: 500
                }}
              >
                Reject Request
              </button>
              {scopePath && (
                <>
                  <button
                    onClick={() => approveWithEdits('file')}
                disabled={decisionQueued}

                    style={{
                      padding: '6px 14px',
                      borderRadius: '8px',
                      background: 'var(--bg-hover)',
                      color: 'var(--text-primary)',
                      border: '1px solid var(--border-strong)',
                      cursor: 'pointer',
                      fontSize: '12px',
                      fontWeight: 500
                    }}
                  >
                    Approve + remember file
                  </button>
                  <button
                    onClick={() => approveWithEdits('folder')}
                disabled={decisionQueued}

                    style={{
                      padding: '6px 14px',
                      borderRadius: '8px',
                      background: 'var(--bg-hover)',
                      color: 'var(--text-primary)',
                      border: '1px solid var(--border-strong)',
                      cursor: 'pointer',
                      fontSize: '12px',
                      fontWeight: 500
                    }}
                  >
                    Approve + remember folder
                  </button>
                </>
              )}
              <button 
                onClick={() => approveWithEdits()}
                disabled={decisionQueued}

                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  background: 'var(--accent)',
                  color: 'var(--accent-on)',
                  border: 'none',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: 600
                }}
              >
                Approve & Execute
              </button>
            </div>
          </div>
        ) : isPending ? (
          <div className="tool-awaiting-result">
            <span className="pulse-dot" />
            <span>Executing tool in sandbox... awaiting output payload</span>
          </div>
        ) : (
          <PayloadDrawer 
            title="📤 Output Payload" 
            data={result} 
            icon={ArrowUpRight}
            defaultOpen={true}
            emptyText="(Empty result)"
          />
        )}
      </div>
    </div>
  );
}

export function NodeActivityCard({ activity, nestedTools = [], rawDelta, onOpenArtifact = null }) {
  if (!activity) return null;

  const { from_node, to_node, action, reason, briefing, input_payload, output_payload, status, delegation_count } = activity;

  const fromStyle = getNodeStyle(from_node);
  const toStyle = getNodeStyle(to_node);

  return (
    <div className={`node-activity-card ${status === 'pending' ? 'pending' : ''}`}>
      <div className="node-activity-header">
        <div className="node-transition-flow">
          <span className="node-badge" style={{ color: fromStyle.color, background: fromStyle.bg, border: `1px solid ${fromStyle.border}` }}>
            {fromStyle.name}
          </span>
          <ArrowRight size={13} className="node-arrow-icon" />
          <span className="node-badge" style={{ color: toStyle.color, background: toStyle.bg, border: `1px solid ${toStyle.border}` }}>
            {toStyle.name}
          </span>
        </div>
        {action && (
          <span className="node-action-pill">
            {action.replace('_', ' ').toUpperCase()}
          </span>
        )}
        {/* S12: the delegation ordinal comes from the engine's S4
            counter on the event payload — the card displays it, it
            never counts cards itself. */}
        {(action === 'delegation' || action === 'delegation_denied') &&
          typeof delegation_count === 'number' && delegation_count > 0 && (
          <span style={{ fontSize: '10.5px', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
            Delegation {delegation_count}
          </span>
        )}
      </div>

      {(reason || briefing) && (
        <div className="node-intent-banner">
          {reason && <div className="node-intent-reason"><strong style={{ color: 'var(--accent)' }}>Goal:</strong> {reason}</div>}
          {briefing && <div className="node-intent-briefing"><strong style={{ color: 'var(--text-secondary)' }}>Briefing:</strong> {briefing}</div>}
        </div>
      )}

      {rawDelta && (
        <div className="node-raw-delta selectable-text">
          {rawDelta.trim()}
        </div>
      )}

      <div className="node-drawers-container">
        {input_payload && Object.keys(input_payload).length > 0 && (
          <PayloadDrawer 
            title="📥 Input State / Payload" 
            data={input_payload} 
            icon={ArrowDownRight}
            defaultOpen={false}
          />
        )}

        {nestedTools && nestedTools.length > 0 && (
          <div className="nested-tools-wrapper">
            <div className="nested-tools-header">
              <TerminalSquare size={12} color="#F5B942" />
              <span>Nested Tool Calls ({nestedTools.length})</span>
            </div>
            <div className="nested-tools-list">
              {nestedTools.map((t, tIdx) => (
                <ToolActivityCard key={tIdx} payload={t.payload} isNested={true} onOpenArtifact={onOpenArtifact} />
              ))}
            </div>
          </div>
        )}

        {output_payload && Object.keys(output_payload).length > 0 && (
          <PayloadDrawer 
            title="📤 Output State / Payload" 
            data={output_payload} 
            icon={ArrowUpRight}
            defaultOpen={false}
          />
        )}
      </div>
    </div>
  );
}

export function ThinkingAccordion({ thoughts = [], isThinking, hasError, onStopTask, onApproveTool = null, pendingApprovals = [], onOpenArtifact = null, envelopeGrants = {}, onEnvelopeGrantChange = null, desktopGrantChoices = {}, onDesktopGrantChoiceChange = null }) {
  let parsedThoughts = thoughts;
  if (typeof thoughts === 'string') {
    try {
      parsedThoughts = JSON.parse(thoughts);
    } catch (e) {
      parsedThoughts = [];
    }
  }
  if (!Array.isArray(parsedThoughts)) {
    parsedThoughts = [];
  }

  // Auto-expand during live execution per grill-me decision
  const [isExpanded, setIsExpanded] = useState(isThinking);
  const scrollRef = useRef(null);

  useSmoothScroll(scrollRef, isExpanded);

  useEffect(() => {
    if (isThinking) {
      setIsExpanded(true);
    }
  }, [isThinking]);

  // If there are no thoughts and the task has finished successfully, don't render an empty box
  if ((!parsedThoughts || parsedThoughts.length === 0) && !isThinking && !hasError) return null;

  // Build hierarchical thought tree
  const treeNodes = [];
  let currentGroup = null;

  parsedThoughts.forEach((item, idx) => {
    if (item.type === 'NODE_ACTIVITY') {
      const action = item.payload?.action;
      // Filter out redundant tool_request/tool_result node activities (handled directly by TOOL_ACTIVITY)
      if (action === 'tool_request' || action === 'tool_result') {
        return;
      }
      currentGroup = {
        type: 'node',
        activity: item.payload,
        tools: [],
        rawDelta: null,
        key: `node-${idx}-${item.payload?.from_node}-${item.payload?.to_node}`
      };
      treeNodes.push(currentGroup);
    } else if (item.type === 'TOOL_ACTIVITY') {
      const toolName = item.payload?.tool_name;
      // Check if tool already exists in treeNodes (e.g. was pending and is now updated)
      const existingIdx = treeNodes.findIndex(
        n => n.type === 'tool' && n.payload?.tool_name === toolName && n.payload?.status === 'pending'
      );
      if (existingIdx !== -1 && item.payload?.status !== 'pending') {
        const prevArgs = treeNodes[existingIdx].payload?.tool_args;
        const newArgs = item.payload?.tool_args;
        const finalArgs = (newArgs && Object.keys(newArgs).length > 0) ? newArgs : (prevArgs || {});
        treeNodes[existingIdx] = {
          type: 'tool',
          payload: { ...treeNodes[existingIdx].payload, ...item.payload, tool_args: finalArgs },
          key: treeNodes[existingIdx].key
        };
      } else {
        treeNodes.push({
          type: 'tool',
          payload: item.payload,
          key: `tool-${idx}-${toolName || 'unknown'}`
        });
      }
    } else {
      const text = item.text?.trim() || '';
      // D1-C10: thought visibility is not decided by content
      // sniffing. A hard-coded greeting prefix used to hide thoughts
      // here, and a '[Supervisor] -> Delegating' prefix drop sat
      // beside it; both are gone — the greeting one by D1-C10, the
      // delegation one by S12, which made delegation structural at
      // the engine (NODE_ACTIVITY only, never a text thought), so
      // there is nothing left to drop. Thoughts render as written.
      if (!text) {
        return;
      }
      if (currentGroup && !currentGroup.rawDelta && currentGroup.activity?.from_node === item.node) {
        currentGroup.rawDelta = text;
      } else {
        treeNodes.push({
          type: 'text',
          node: item.node,
          text: text,
          key: `text-${idx}`
        });
      }
    }
  });

  // D1-C1: bind pending tool cards to pending approvals by CALL
  // ORDER, not by tool name alone. The wire carries no per-call
  // id (TOOL_ACTIVITY and TOOL_APPROVAL_REQUEST payloads share
  // only tool_name / args / task_id), and the engine raises
  // approvals in call order — it interrupts on each call before
  // running the next — so the k-th still-pending card for a tool
  // owns the k-th undecided approval for that tool. The old
  // name-only `find` bound EVERY card to the first approval: a
  // card displaying call B's arguments approved call A.
  const approvalByCardKey = new Map();
  {
    const queuesByTool = new Map();
    (pendingApprovals || []).forEach((a) => {
      const queue = queuesByTool.get(a.tool_name) || [];
      queue.push(a);
      queuesByTool.set(a.tool_name, queue);
    });
    treeNodes.forEach((item) => {
      if (item.type === 'tool' && item.payload?.status === 'pending') {
        const queue = queuesByTool.get(item.payload?.tool_name);
        if (queue && queue.length > 0) {
          approvalByCardKey.set(item.key, queue.shift());
        }
      }
    });
  }

  if (treeNodes.length === 0 && !isThinking && !hasError) return null;

  return (
    <div style={{
      marginBottom: '12px',
      borderRadius: 'var(--radius-lg)',
      background: 'var(--bg-raised)',
      border: isExpanded ? '1px solid var(--border-strong)' : '1px solid var(--border-default)',
      overflow: 'hidden',
      transition: 'all 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
      boxShadow: 'none'
    }}>
      <div 
        onClick={() => setIsExpanded(!isExpanded)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '9px 13px',
          background: isExpanded ? 'var(--bg-overlay)' : 'transparent',
          color: isThinking ? 'var(--accent)' : 'var(--text-secondary)',
          fontSize: '12px',
          cursor: 'pointer',
          userSelect: 'none',
          transition: 'background 0.2s',
        }}
        title="Click to inspect node-to-node handoffs and tool payloads"
      >
        {isThinking ? (
          <div className="spinner" style={{ 
            width: '12px', height: '12px', 
            border: '2px solid rgba(var(--accent-rgb), 0.3)',
            borderTopColor: 'var(--accent)', 
            borderRadius: '50%',
            animation: 'spin 1s linear infinite',
            flexShrink: 0
          }} />
        ) : hasError ? (
          <X size={13} color="#C92A2A" style={{ flexShrink: 0 }} />
        ) : (
          <CheckCircle2 size={13} color="#4CC38A" style={{ flexShrink: 0 }} />
        )}
        
        <span style={{ flex: 1, textAlign: 'left', fontWeight: '500', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span>{isThinking ? 'Agent is thinking...' : hasError ? 'Task Stopped' : `Execution Pipeline (${treeNodes.length} steps)`}</span>
          <span style={{ fontSize: '10.5px', opacity: 0.65, fontWeight: 400, color: 'var(--text-muted)' }}>
            {isExpanded ? '(click to collapse)' : '(click to inspect payloads)'}
          </span>
        </span>

        <ChevronDown 
          size={14} 
          style={{ 
            transition: 'transform 0.3s ease',
            transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
            opacity: 0.8
          }} 
        />
      </div>
      
      {isExpanded && (
        <div 
          ref={scrollRef}
          style={{
            padding: '12px',
            fontSize: '12px',
            color: 'var(--text-primary)',
            maxHeight: '380px',
            overflowY: 'auto',
            borderTop: '1px solid var(--border-default)',
            background: 'var(--bg-base)'
          }} 
          className="chat-thought-container chat-scroll"
        >
          {(!treeNodes || treeNodes.length === 0) ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', padding: '6px 0', color: 'var(--text-secondary)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: hasError ? 'var(--danger)' : 'var(--accent)' }}>
                {!hasError && <span className="dot-typing" style={{ width: '5px', height: '5px', display: 'inline-block' }}></span>}
                <span>{hasError ? 'Task was stopped before supervisor node could initialize.' : 'Initializing supervisor node & analyzing prompt requirements...'}</span>
              </div>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                Evaluating available specialists, memory context, and provider route...
              </div>
            </div>
          ) : (
            treeNodes.map((item) => {
              if (item.type === 'node') {
                return (
                  <NodeActivityCard 
                    key={item.key} 
                    activity={item.activity} 
                    nestedTools={item.tools} 
                    rawDelta={item.rawDelta} 
                    onOpenArtifact={onOpenArtifact}
                  />
                );
              }
              if (item.type === 'tool') {
                const approval = approvalByCardKey.get(item.key) || null;
                return (
                  <ToolActivityCard
                    key={item.key}
                    payload={item.payload}
                    pendingApproval={approval}
                    onApproveTool={onApproveTool}
                    envelopeGrantChecked={!!(approval && envelopeGrants[approval.id])}
                    onEnvelopeGrantCheckedChange={approval && onEnvelopeGrantChange ? (checked) => onEnvelopeGrantChange(approval.id, checked) : null}
                    desktopGrantChoice={(approval && desktopGrantChoices[approval.id]) || {}}
                    onDesktopGrantChoiceChange={approval && onDesktopGrantChoiceChange ? (field, checked) => onDesktopGrantChoiceChange(approval.id, field, checked) : null}
                    onOpenArtifact={onOpenArtifact}
                  />
                );
              }
              // Text item
              const nodeStyle = getNodeStyle(item.node);
              return (
                <div key={item.key} style={{ marginBottom: '10px', padding: '8px 10px', borderRadius: '8px', background: 'var(--bg-raised)', border: '1px solid var(--border-default)' }}>
                  <span style={{ 
                    color: nodeStyle.color, 
                    background: nodeStyle.bg,
                    border: `1px solid ${nodeStyle.border}`,
                    padding: '2px 7px',
                    borderRadius: '4px',
                    fontSize: '10px',
                    fontWeight: 600,
                    letterSpacing: '0.5px'
                  }}>
                    {nodeStyle.name}
                  </span>
                  <div className="thought-step-content selectable-text" style={{ marginTop: '6px', whiteSpace: 'pre-wrap', opacity: 0.9, fontSize: '11.5px', userSelect: 'text', cursor: 'text' }}>
                    {item.text?.trim()}
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
