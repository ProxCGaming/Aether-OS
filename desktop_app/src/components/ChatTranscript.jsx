import React from 'react';
import ReactMarkdown from 'react-markdown';
import { AlertCircle, ShieldAlert, X, TerminalSquare, User, Bot, Edit2, MoreHorizontal, RotateCcw, Volume2, VolumeX, KeyRound } from 'lucide-react';
import {
  ThinkingAccordion,
  ApprovalPreview,
  BrowserConfirmationControl,
  DesktopConfirmationControl,
  approvalScopePath,
  getRewindPointForMessage,
} from './ChatCards';
import { DEFAULT_SLOW_TEXT } from './Settings/LoadStateRow';

// Wave 5 (Annex C row 6, transcript seam): the message-list
// rendering — the feed, its per-role rows (system notices, the
// standalone approval cards, chat bubbles with their step menus),
// and the voice live-mirror — extracted from
// DraggableChatWindow. Presentational: feed state and every
// handler stay in the window, which passes them down as props.
// Rows are keyed by the caller (D1-C9) with the message's stable
// id, so the row components render their root element keyless.

function SystemMessageRow({ msg }) {
  return (
    <div data-message-id={msg.id || undefined} style={{ 
      display: 'flex', 
      alignItems: 'center', 
      gap: '12px', 
      margin: '8px 0', 
      padding: '12px 16px', 
      background: 'var(--bg-raised)', 
      borderRadius: 'var(--radius-lg)',
      color: 'var(--text-secondary)',
      fontSize: '13px'
    }}>
      <AlertCircle size={16} />
      <span style={{ flex: 1 }}>{msg.content}</span>
    </div>
  );
}

function ToolApprovalMessage({
  msg,
  approvalArgEdits,
  setApprovalArgEdits,
  browserEnvelopeGrants,
  setBrowserEnvelopeGrants,
  desktopGrantChoices,
  setDesktopGrantChoices,
  onApproveTool,
  approveToolWithEdits,
}) {
  return (
    <div data-message-id={msg.id || undefined} style={{ 
      margin: '8px 0', 
      background: 'var(--bg-raised)', 
      border: '1px solid rgba(var(--accent-rgb), 0.35)',
      borderRadius: 'var(--radius-lg)',
      overflow: 'hidden',
      boxShadow: 'none'
    }}>
      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', borderBottom: '1px solid var(--border-default)', background: 'rgba(var(--accent-rgb), 0.1)' }}>
        <ShieldAlert size={20} color="#F5B942" />
        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: 'var(--accent)' }}>Execution Approval Required</h3>
      </div>
      <div style={{ padding: '20px' }}>
        <p style={{ margin: '0 0 16px 0', fontSize: '14px', color: 'var(--text-primary)', lineHeight: 1.5 }}>
          The agent has requested permission to execute <strong>{msg.tool_name}</strong>.
        </p>
        <div style={{ background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '20px', overflowX: 'auto' }}>
          {JSON.stringify(msg.tool_args, null, 2)}
        </div>
        {msg.preview && (
          <div style={{ marginBottom: '20px' }}>
            <ApprovalPreview preview={msg.preview} />
          </div>
        )}
        {msg.browser_confirmation && (
          <div style={{ marginBottom: '20px' }}>
            <BrowserConfirmationControl
              confirmation={msg.browser_confirmation}
              grantChecked={!!browserEnvelopeGrants[msg.id]}
              onGrantCheckedChange={(checked) => setBrowserEnvelopeGrants(prev => ({ ...prev, [msg.id]: checked }))}
            />
          </div>
        )}
        {msg.desktop_confirmation && (
          <div style={{ marginBottom: '20px' }}>
            <DesktopConfirmationControl
              confirmation={msg.desktop_confirmation}
              choice={(desktopGrantChoices && desktopGrantChoices[msg.id]) || {}}
              onChoiceChange={(field, checked) => setDesktopGrantChoices && setDesktopGrantChoices(prev => ({
                ...prev,
                [msg.id]: { ...((prev && prev[msg.id]) || {}), [field]: checked },
              }))}
            />
          </div>
        )}
        {!approvalArgEdits[msg.id] ? (
          <button
            data-testid="approval-edit-args-btn"
            onClick={() => setApprovalArgEdits(prev => ({ ...prev, [msg.id]: { text: JSON.stringify(msg.tool_args || {}, null, 2), error: '' } }))}
            disabled={!!msg.decisionQueued}
            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', marginBottom: '20px' }}>
            Edit arguments
          </button>
        ) : (
          <div style={{ marginBottom: '20px' }}>
            <textarea
              data-testid="approval-args-editor"
              value={approvalArgEdits[msg.id].text}
              onChange={(e) => setApprovalArgEdits(prev => ({ ...prev, [msg.id]: { text: e.target.value, error: '' } }))}
              rows={6}
              spellCheck={false}
              style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', color: 'var(--text-primary)', border: '1px solid var(--border-default)', borderRadius: '8px', padding: '10px', fontFamily: 'var(--font-mono)', fontSize: '12px' }}
            />
            {approvalArgEdits[msg.id].error && (
              <div data-testid="approval-args-error" style={{ fontSize: '12px', color: 'var(--danger)', marginTop: '6px' }}>{approvalArgEdits[msg.id].error}</div>
            )}
          </div>
        )}
        {msg.decisionQueued && (
          <div data-testid="approval-decision-queued" style={{ marginBottom: '16px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(var(--accent-rgb), 0.15)', border: '1px solid rgba(var(--accent-rgb), 0.35)', color: 'var(--accent)', fontSize: '12px' }}>
            {msg.decisionApproved ? 'Approval' : 'Rejection'} queued — will send when the engine reconnects.
          </div>
        )}
        <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button 
            onClick={() => onApproveTool && onApproveTool(msg.id, false, msg.approval_key)}
            disabled={!!msg.decisionQueued}
            className="ios-close-btn"
            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', display: 'flex', alignItems: 'center' }}
          >
            <X size={16} style={{ marginRight: '6px' }} />
            Reject Request
          </button>
          {approvalScopePath(msg.tool_args) && (
            <>
              <button 
                onClick={() => approveToolWithEdits(msg, 'file')}
            disabled={!!msg.decisionQueued}

                className="ios-close-btn"
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', display: 'flex', alignItems: 'center' }}
              >
                Approve + remember file
              </button>
              <button 
                onClick={() => approveToolWithEdits(msg, 'folder')}
            disabled={!!msg.decisionQueued}

                className="ios-close-btn"
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', display: 'flex', alignItems: 'center' }}
              >
                Approve + remember folder
              </button>
            </>
          )}
          <button 
            onClick={() => approveToolWithEdits(msg)}
            disabled={!!msg.decisionQueued}

            className="ios-close-btn"
            style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
          >
            <TerminalSquare size={16} style={{ marginRight: '6px' }} />
            Approve Execution
          </button>
        </div>
      </div>
    </div>
  );
}

function PluginApprovalMessage({ msg, onApprovePlugin }) {
  return (
    <div data-message-id={msg.id || undefined} style={{ 
      margin: '8px 0', 
      background: 'var(--bg-raised)', 
      border: '1px solid rgba(var(--accent-rgb), 0.35)',
      borderRadius: 'var(--radius-lg)',
      overflow: 'hidden',
      boxShadow: 'none'
    }}>
      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', borderBottom: '1px solid var(--border-default)', background: 'rgba(var(--accent-rgb), 0.1)' }}>
        <ShieldAlert size={20} color="#F5B942" />
        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: 'var(--accent)' }}>Plugin Install Approval Required</h3>
      </div>
      <div style={{ padding: '20px' }}>
        <p style={{ margin: '0 0 16px 0', fontSize: '14px', color: 'var(--text-primary)', lineHeight: 1.5 }}>
          The agent has requested permission to install plugin <strong>{msg.plugin_name}</strong>.
        </p>
        <div style={{ background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '20px', overflowX: 'auto' }}>
          {JSON.stringify(msg.plugin_details, null, 2)}
        </div>
        {msg.decisionQueued && (
          <div data-testid="approval-decision-queued" style={{ marginBottom: '16px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(var(--accent-rgb), 0.15)', border: '1px solid rgba(var(--accent-rgb), 0.35)', color: 'var(--accent)', fontSize: '12px' }}>
            {msg.decisionApproved ? 'Approval' : 'Rejection'} queued — will send when the engine reconnects.
          </div>
        )}
        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
          <button 
            onClick={() => onApprovePlugin && onApprovePlugin(msg.id, false)}
            disabled={!!msg.decisionQueued}
            className="ios-close-btn"
            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', display: 'flex', alignItems: 'center' }}
          >
            <X size={16} style={{ marginRight: '6px' }} />
            Reject Request
          </button>
          <button 
            onClick={() => onApprovePlugin && onApprovePlugin(msg.id, true)}
            disabled={!!msg.decisionQueued}
            className="ios-close-btn"
            style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
          >
            <TerminalSquare size={16} style={{ marginRight: '6px' }} />
            Approve Install
          </button>
        </div>
      </div>
    </div>
  );
}

// F2 (post-Wave-6 linkage audit): the vault ask-every-time prompt.
// Same amber approval pattern as the tool/plugin cards above. The
// engine's push carries site + username only — a password is never
// in the payload and never rendered here; it goes straight into the
// page if (and only if) the owner approves. An expired card (the
// engine's timeout resolved the fill closed before an answer) shows
// the refusal and disables its buttons instead of offering a stale
// answer as if it were live.
function VaultFillApprovalMessage({ msg, onVaultFillApproval }) {
  const decided = !!msg.decisionQueued;
  const expired = !!msg.expired;
  return (
    <div data-testid="vault-fill-approval-card" data-message-id={msg.id || undefined} style={{
      margin: '8px 0',
      background: 'var(--bg-raised)',
      border: '1px solid rgba(var(--accent-rgb), 0.35)',
      borderRadius: 'var(--radius-lg)',
      overflow: 'hidden',
      boxShadow: 'none'
    }}>
      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', borderBottom: '1px solid var(--border-default)', background: 'rgba(var(--accent-rgb), 0.1)' }}>
        <ShieldAlert size={20} color="#F5B942" />
        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: 'var(--accent)' }}>Credential Fill Approval Required</h3>
      </div>
      <div style={{ padding: '20px' }}>
        <p style={{ margin: '0 0 16px 0', fontSize: '14px', color: 'var(--text-primary)', lineHeight: 1.5 }}>
          The agent wants to fill a saved vault credential: <strong>{msg.username}</strong> on <strong>{msg.site}</strong>. Because this entry is set to ask every time, the fill waits for your answer. The password itself is never shown here — it goes directly into the page only if you approve.
        </p>
        {msg.decisionQueued && (
          <div data-testid="approval-decision-queued" style={{ marginBottom: '16px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(var(--accent-rgb), 0.15)', border: '1px solid rgba(var(--accent-rgb), 0.35)', color: 'var(--accent)', fontSize: '12px' }}>
            {msg.decisionApproved ? 'Approval' : 'Denial'} queued — will send when the engine reconnects.
          </div>
        )}
        {expired && !decided && (
          <div data-testid="vault-fill-approval-expired" style={{ marginBottom: '16px', padding: '8px 12px', borderRadius: '8px', background: 'rgba(201, 42, 42, 0.12)', border: '1px solid rgba(201, 42, 42, 0.35)', color: 'var(--danger)', fontSize: '12px' }}>
            This request expired — the fill was refused. If you still want it filled, ask the agent to try again.
          </div>
        )}
        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
          <button
            data-testid="vault-fill-deny-btn"
            onClick={() => onVaultFillApproval && onVaultFillApproval(msg.id, false)}
            disabled={decided || expired}
            className="ios-close-btn"
            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: 'var(--text-primary)', display: 'flex', alignItems: 'center' }}
          >
            <X size={16} style={{ marginRight: '6px' }} />
            Deny Fill
          </button>
          <button
            data-testid="vault-fill-approve-btn"
            onClick={() => onVaultFillApproval && onVaultFillApproval(msg.id, true)}
            disabled={decided || expired}
            className="ios-close-btn"
            style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
          >
            <KeyRound size={16} style={{ marginRight: '6px' }} />
            Approve Fill
          </button>
        </div>
      </div>
    </div>
  );
}

function ChatMessageRow({
  msg,
  index,
  messages,
  rewindManifest,
  openStepMenu,
  setOpenStepMenu,
  onRewindRequest,
  onForkRequest,
  onEditMessage,
  setInput,
  textareaRef,
  onStopTask,
  onApproveTool,
  pendingApprovals,
  browserEnvelopeGrants,
  onEnvelopeGrantChange,
  desktopGrantChoices,
  onDesktopGrantChoiceChange,
  onOpenArtifact,
  playingMessageId,
  readAloudError,
  onToggleReadAloud,
  isThinking,
  isLastFinalAgent,
  onRegenerate,
}) {
  // Epic 8, E8-1.1a: this message's recorded rewind
  // point for the step menu (null without a manifest).
  const rewindPoint = rewindManifest
    ? getRewindPointForMessage(messages, index, rewindManifest)
    : null;

  return (
  <div data-message-id={msg.id || undefined} className={`chat-message ${msg.role === 'user' ? 'user' : 'agent'}`} style={{
    display: 'flex',
    flexDirection: msg.role === 'user' ? 'row-reverse' : 'row',
    gap: '12px',
    alignItems: 'flex-start'
  }}>
    <div style={{
      width: '32px',
      height: '32px',
      borderRadius: '10px',
      background: msg.role === 'user' ? 'var(--bg-hover)' : 'rgba(var(--accent-rgb), 0.2)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      flexShrink: 0
    }}>
      {msg.role === 'user' ? <User size={18} color="#F4F4F2" /> : <Bot size={18} color="#F5B942" />}
    </div>
    <div className={`chat-bubble ${msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-agent'} msg-container`} style={{ position: 'relative' }}>
      {msg.role === 'user' && (
        <button
          type="button"
          className="msg-edit-btn"
          title="Edit and resend"
          aria-label="Edit and resend this message"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            if (onEditMessage) onEditMessage(msg, index);
            const text = typeof msg.content === 'string' ? msg.content : '';
            setInput(text);
            // Focus after React applies the new input
            // value, and put the caret at the end so the
            // user can keep typing immediately.
            requestAnimationFrame(() => {
              const ta = textareaRef.current;
              if (ta) {
                ta.focus();
                const end = ta.value.length;
                try { ta.setSelectionRange(end, end); } catch (err) {}
              }
            });
          }}
        >
          <Edit2 size={12} />
        </button>
      )}
      {(
        <button
          type="button"
          className="msg-step-menu-btn"
          data-testid="step-menu-btn"
          title="Step actions"
          aria-label="Step actions for this message"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setOpenStepMenu(openStepMenu === index ? null : index);
          }}
        >
          <MoreHorizontal size={12} />
        </button>
      )}
      {openStepMenu === index && (
        <div className="step-menu" data-testid="step-menu">
          {rewindPoint && rewindPoint.scope === 'current' && rewindPoint.entry && (
            <div className="rewind-point-meta" data-testid="rewind-point-meta">
              <div className="rewind-point-label">{rewindPoint.entry.label}</div>
              <div className="rewind-point-detail">
                Checkpoint {String(rewindPoint.entry.checkpoint_id || '').slice(0, 8)}
              </div>
              {Array.isArray(rewindPoint.entry.files_written) && rewindPoint.entry.files_written.length > 0 && (
                <div className="rewind-point-detail">
                  Files written: {rewindPoint.entry.files_written.join(', ')}
                </div>
              )}
            </div>
          )}
          {rewindPoint && (rewindPoint.scope === 'current' && rewindPoint.entry ? (
            <button
              type="button"
              className="step-menu-action"
              data-testid="rewind-action"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setOpenStepMenu(null);
                if (onRewindRequest) onRewindRequest(rewindPoint.entry);
              }}
            >
              Rewind to here
            </button>
          ) : (
            <>
              <button type="button" className="step-menu-action" data-testid="rewind-action" disabled>
                Rewind to here
              </button>
              <div className="step-menu-hint" data-testid="rewind-hint">
                Rewind is available for the current task only.
              </div>
            </>
          ))}
          <button
            type="button"
            className="step-menu-action"
            data-testid="fork-action"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setOpenStepMenu(null);
              if (onForkRequest) {
                onForkRequest({
                  message_id: msg.id || null,
                  step_index: rewindPoint && rewindPoint.scope === 'current' && rewindPoint.entry
                    ? rewindPoint.entry.step_index
                    : null,
                });
              }
            }}
          >
            Fork from here
          </button>
        </div>
      )}
      {(msg.role === 'agent' || msg.role === 'assistant') && (
        <ThinkingAccordion 
          thoughts={msg.thoughts} 
          isThinking={!msg.isFinal} 
          hasError={msg.hasError} 
          onStopTask={onStopTask} 
          onApproveTool={onApproveTool}
          pendingApprovals={pendingApprovals}
          envelopeGrants={browserEnvelopeGrants}
          onEnvelopeGrantChange={onEnvelopeGrantChange}
          desktopGrantChoices={desktopGrantChoices}
          onDesktopGrantChoiceChange={onDesktopGrantChoiceChange}
          onOpenArtifact={onOpenArtifact}
        />
      )}

      {Array.isArray(msg.attachments) && msg.attachments.length > 0 && (
        <div className="msg-attachments" data-testid="message-attachments" style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: msg.content ? '8px' : '0' }}>
          {msg.attachments.map((att, attIdx) => {
            const src = att.data || att.url || (att.path ? (att.path.startsWith('data:') ? att.path : `file://${att.path}`) : '');
            return (
              <div key={attIdx} className="msg-attachment-item" style={{ maxWidth: '240px', borderRadius: '8px', overflow: 'hidden' }}>
                {src ? (
                  <img src={src} alt={att.name || 'Attachment'} style={{ maxWidth: '100%', maxHeight: '200px', objectFit: 'contain', display: 'block', borderRadius: '8px' }} />
                ) : (
                  <div style={{ padding: '4px 8px', background: 'var(--bg-hover)', borderRadius: '6px', fontSize: '12px' }}>
                    {att.name || 'Attachment'}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {msg.content ? (
        <ReactMarkdown>
          {msg.content
            .replace(/<thought>/g, '```\n')
            .replace(/<\/thought>/g, '\n```')
            .replace(/<thinking>/g, '```\n')
            .replace(/<\/thinking>/g, '\n```')}
        </ReactMarkdown>
      ) : (
        (msg.role === 'agent' || msg.role === 'assistant') && !msg.isFinal && (
          <div style={{ display: 'flex', gap: '4px', alignItems: 'center', height: '24px' }}>
            <span className="dot-typing"></span>
            <span className="dot-typing" style={{ animationDelay: '0.2s' }}></span>
            <span className="dot-typing" style={{ animationDelay: '0.4s' }}></span>
          </div>
        )
      )}
      {(msg.queued || msg.steering) && (
        <div
          className="msg-queued-indicator"
          data-testid="queued-indicator"
          style={{ marginTop: '6px', fontSize: '10px', letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--text-muted)' }}
        >
          Queued
        </div>
      )}
      {(msg.role === 'agent' || msg.role === 'assistant') && msg.content && (
        <button
          type="button"
          className={`msg-read-aloud-btn ${playingMessageId === String(msg.id || index) ? 'playing' : ''}`}
          data-testid={`read-aloud-btn-${msg.id || index}`}
          title={playingMessageId === String(msg.id || index) ? 'Stop read aloud' : (readAloudError && playingMessageId === String(msg.id || index) ? readAloudError : 'Read aloud')}
          aria-label={playingMessageId === String(msg.id || index) ? 'Stop read aloud' : 'Read message aloud'}
          aria-pressed={playingMessageId === String(msg.id || index) ? 'true' : 'false'}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onToggleReadAloud(msg, index);
          }}
          style={{
            background: playingMessageId === String(msg.id || index) ? 'rgba(var(--accent-rgb), 0.2)' : 'var(--bg-hover)',
            border: playingMessageId === String(msg.id || index) ? '1px solid rgba(var(--accent-rgb), 0.4)' : '1px solid var(--border-default)',
            borderRadius: '6px',
            color: playingMessageId === String(msg.id || index) ? 'var(--accent)' : 'var(--text-secondary)',
            cursor: 'pointer',
            display: 'inline-flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '11px',
            padding: '3px 8px',
            marginTop: '6px',
            marginRight: '6px',
            transition: 'all 0.15s ease',
          }}
        >
          {playingMessageId === String(msg.id || index) ? <VolumeX size={12} color="#F5B942" /> : <Volume2 size={12} />}
          <span>{playingMessageId === String(msg.id || index) ? 'Stop' : 'Read'}</span>
        </button>
      )}
      {readAloudError && (
        <div
          data-testid="read-aloud-error"
          style={{ color: 'var(--danger)', fontSize: '11px', marginTop: '4px', padding: '4px 8px', background: 'rgba(201, 42, 42, 0.12)', borderRadius: '4px' }}
        >
          {readAloudError}
        </div>
      )}
      {isLastFinalAgent && !isThinking && onRegenerate && (
        <button
          type="button"
          className="msg-regenerate-btn"
          title="Regenerate response"
          aria-label="Regenerate response"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onRegenerate();
          }}
        >
          <RotateCcw size={12} />
          <span>Regenerate</span>
        </button>
      )}
    </div>
  </div>
  );
}

export default function ChatTranscript({
  messages,
  isThinking,
  sessionSwitchPending,
  chatOffline,
  switchSlow,
  voiceState,
  feedScrollRef,
  onFeedScroll,
  messagesEndRef,
  rewindManifest,
  onRewindRequest,
  onForkRequest,
  approvalArgEdits,
  setApprovalArgEdits,
  browserEnvelopeGrants,
  setBrowserEnvelopeGrants,
  onEnvelopeGrantChange,
  desktopGrantChoices,
  setDesktopGrantChoices,
  onDesktopGrantChoiceChange,
  onApproveTool,
  onApprovePlugin,
  onVaultFillApproval,
  approveToolWithEdits,
  onStopTask,
  onOpenArtifact,
  onEditMessage,
  setInput,
  textareaRef,
  openStepMenu,
  setOpenStepMenu,
  playingMessageId,
  readAloudError,
  onToggleReadAloud,
  onRegenerate,
}) {
  const pendingApprovals = messages.filter((m) => m.role === 'tool_approval');

  // Epic 1 UI batch (E1-08): regenerate is offered on the last final
  // agent bubble only (error bubbles included), and never mid-task.
  const lastFinalAgentIndex = messages.findLastIndex(
    (m) => (m.role === 'agent' || m.role === 'assistant') && m.isFinal
  );

  return (
    <div className="chat-scroll" ref={feedScrollRef} onScroll={onFeedScroll} style={{
      flex: 1,
      padding: '20px',
      overflowY: 'auto',
      display: 'flex',
      flexDirection: 'column',
      gap: '16px'
    }}>
      {messages.length === 0 && !isThinking && (
        sessionSwitchPending ? (
          <div data-testid="session-switch-loading" style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '6px', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)', textAlign: 'center' }}>
            <p style={{ margin: 0 }}>{chatOffline ? 'Waiting for the engine — this session will load once it connects.' : 'Loading session…'}</p>
            {switchSlow && <p style={{ margin: 0, fontSize: '12.5px' }}>{DEFAULT_SLOW_TEXT}</p>}
          </div>
        ) : (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)', textAlign: 'center' }}>
          <p>No active session. Open a project or start a new chat.</p>
        </div>
        )
      )}
      {/* D1-C9: rows are keyed by the message's stable id
          (index only as a fallback for the rare id-less
          local bubble) — index keys handed one message's
          row state (e.g. an expanded pipeline) to
          whichever message landed at that index after
          the list was cut by an edit, rewind or fork. */}
      {messages.map((msg, i) => {
        if (msg.role === 'system') {
          return <SystemMessageRow key={msg.id || i} msg={msg} />;
        }
        if (msg.role === 'tool_approval') {
          return (
            <ToolApprovalMessage
              key={msg.id || i}
              msg={msg}
              approvalArgEdits={approvalArgEdits}
              setApprovalArgEdits={setApprovalArgEdits}
              browserEnvelopeGrants={browserEnvelopeGrants}
              setBrowserEnvelopeGrants={setBrowserEnvelopeGrants}
              desktopGrantChoices={desktopGrantChoices}
              setDesktopGrantChoices={setDesktopGrantChoices}
              onApproveTool={onApproveTool}
              approveToolWithEdits={approveToolWithEdits}
            />
          );
        }
        if (msg.role === 'plugin_approval') {
          return (
            <PluginApprovalMessage
              key={msg.id || i}
              msg={msg}
              onApprovePlugin={onApprovePlugin}
            />
          );
        }
        if (msg.role === 'vault_fill_approval') {
          return (
            <VaultFillApprovalMessage
              key={msg.id || i}
              msg={msg}
              onVaultFillApproval={onVaultFillApproval}
            />
          );
        }
        return (
          <ChatMessageRow
            key={msg.id || i}
            msg={msg}
            index={i}
            messages={messages}
            rewindManifest={rewindManifest}
            openStepMenu={openStepMenu}
            setOpenStepMenu={setOpenStepMenu}
            onRewindRequest={onRewindRequest}
            onForkRequest={onForkRequest}
            onEditMessage={onEditMessage}
            setInput={setInput}
            textareaRef={textareaRef}
            onStopTask={onStopTask}
            onApproveTool={onApproveTool}
            pendingApprovals={pendingApprovals}
            browserEnvelopeGrants={browserEnvelopeGrants}
            onEnvelopeGrantChange={onEnvelopeGrantChange}
            desktopGrantChoices={desktopGrantChoices}
            onDesktopGrantChoiceChange={onDesktopGrantChoiceChange}
            onOpenArtifact={onOpenArtifact}
            playingMessageId={playingMessageId}
            readAloudError={readAloudError}
            onToggleReadAloud={onToggleReadAloud}
            isThinking={isThinking}
            isLastFinalAgent={i === lastFinalAgentIndex}
            onRegenerate={onRegenerate}
          />
        );
      })}
      {/* Voice live mirror (Epic 8, E8-5.1a): provisional
          bubbles — the partial transcript and the streaming
          reply. Finals commit as real messages via App. */}
      {voiceState && voiceState.partialText && (
        <div className="chat-message user" data-testid="voice-partial-bubble" style={{ display: 'flex', flexDirection: 'row-reverse', gap: '12px', padding: '16px 20px', opacity: 0.75 }}>
          <div style={{ background: 'rgba(255,255,255,0.1)', borderRadius: '50%', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <User size={18} color="#F4F4F2" />
          </div>
          <div style={{ fontSize: '14px', lineHeight: 1.6, fontStyle: 'italic' }}>{voiceState.partialText}</div>
        </div>
      )}
      {voiceState && voiceState.assistantText && (
        <div className="chat-message agent" data-testid="voice-assistant-bubble" style={{ display: 'flex', gap: '12px', padding: '16px 20px' }}>
          <div style={{ background: 'rgba(var(--accent-rgb), 0.2)', borderRadius: '50%', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <Bot size={18} color="#F5B942" />
          </div>
          <div style={{ fontSize: '14px', lineHeight: 1.6 }}>{voiceState.assistantText}</div>
        </div>
      )}
      <div ref={messagesEndRef} />
    </div>
  );
}
