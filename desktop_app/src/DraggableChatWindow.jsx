import React, { useState, useEffect, useRef } from 'react';

import { Globe, Play, Pause, Send, Maximize2, Minimize2, X, GripHorizontal, Bot, User, AlertCircle, ShieldAlert, Check, Shield, TerminalSquare, ChevronDown, ExternalLink, LogIn, Square, CheckCircle2, Copy, Scissors, Clipboard, CheckCheck, ArrowRight, ArrowDownRight, ArrowUpRight, Cpu, Layers, Activity, Edit2, RotateCcw, Download, MoreHorizontal, CircleDollarSign, Mic } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import Dropdown from './Dropdown';
import UndoToast from './components/UndoToast';
import SnipOverlay from './components/SnipOverlay';
import CostHeaderPopover, { formatUsd } from './components/CostHeaderPopover';
import VoiceOverlay from './components/VoiceOverlay';
import './chat.css';

export function getNodeStyle(nodeName = '') {
  const n = String(nodeName).toLowerCase();
  if (n.includes('supervisor')) {
    return { color: '#c4b5fd', bg: 'rgba(167, 139, 250, 0.16)', border: 'rgba(167, 139, 250, 0.35)', name: 'SUPERVISOR' };
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

  const handleCopy = (e) => {
    e.stopPropagation();
    try {
      navigator.clipboard.writeText(formattedText);
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
            {copied ? <CheckCheck size={11} color="#10b981" /> : <Copy size={11} />}
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
          background: 'rgba(0, 0, 0, 0.45)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          borderRadius: '8px',
          padding: '10px 12px',
          maxHeight: '240px',
          overflowY: 'auto',
          fontFamily: 'monospace',
          fontSize: '12px',
          lineHeight: 1.5,
        }}
      >
        {diffLines.map((line, idx) => {
          let cls = 'diff-line diff-line-context';
          let color = '#cbd5e1';
          let background = 'transparent';
          if (line.startsWith('+++') || line.startsWith('---')) {
            cls = 'diff-line diff-line-meta';
            color = '#e2e8f0';
          } else if (line.startsWith('+')) {
            cls = 'diff-line diff-line-add';
            color = '#4ade80';
            background = 'rgba(74, 222, 128, 0.08)';
          } else if (line.startsWith('-')) {
            cls = 'diff-line diff-line-del';
            color = '#f87171';
            background = 'rgba(248, 113, 113, 0.08)';
          } else if (line.startsWith('@@')) {
            cls = 'diff-line diff-line-hunk';
            color = '#9090a0';
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
          color: '#fbbf24',
          background: 'rgba(245, 158, 11, 0.1)',
          border: '1px solid rgba(245, 158, 11, 0.3)',
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

// Epic 8, E8-1.3: parse the approval drawer's argument editor.
// Returns { editedArgs } only when the parsed JSON is a plain object
// that actually differs from the proposed args; { editedArgs: null }
// for an unchanged editor (legacy approve), and { error } for text
// that must block the approve (invalid JSON / not an object).
function parseApprovalEditedArgs(text, originalArgs) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { editedArgs: null, error: 'Arguments must be valid JSON.' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { editedArgs: null, error: 'Arguments must be a JSON object.' };
  }
  const sortDeep = (v) => (Array.isArray(v)
    ? v.map(sortDeep)
    : (v && typeof v === 'object'
      ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortDeep(v[k])]))
      : v));
  if (JSON.stringify(sortDeep(parsed)) === JSON.stringify(sortDeep(originalArgs || {}))) {
    return { editedArgs: null, error: null };
  }
  return { editedArgs: parsed, error: null };
}


export function ToolActivityCard({ payload, isNested = false, pendingApproval = null, onApproveTool = null, onOpenArtifact = null }) {  // Epic 8, E8-1.3: approval argument editor state (hooks first —
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
  const approveWithEdits = (scope) => {
    if (!onApproveTool || !pendingApproval) return;
    if (!argsEditorOpen) {
      if (scope) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, scope);
      else onApproveTool(pendingApproval.id, true, pendingApproval.approval_key);
      return;
    }
    const { editedArgs, error } = parseApprovalEditedArgs(argsText, approvalArgs);
    if (error) {
      setArgsError(error);
      return;
    }
    if (editedArgs) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, scope, editedArgs);
    else if (scope) onApproveTool(pendingApproval.id, true, pendingApproval.approval_key, scope);
    else onApproveTool(pendingApproval.id, true, pendingApproval.approval_key);
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
              style={{
                background: 'rgba(167, 139, 250, 0.15)',
                border: '1px solid rgba(167, 139, 250, 0.3)',
                color: '#c084fc',
                borderRadius: '4px',
                padding: '1px 6px',
                fontSize: '11px',
                cursor: 'pointer',
                fontFamily: 'monospace'
              }}
            >
              {tool_args.path.split(/[\/]/).pop()}
            </button>
          )}
          <span className="tool-route-badge">
            {callerNode.toUpperCase()} ➔ {targetNode.toUpperCase()}
          </span>
        </div>
        {edited && (
          <span data-testid="tool-edited-badge" style={{ fontSize: '10px', fontWeight: 600, color: '#fbbf24', background: 'rgba(251, 191, 36, 0.15)', border: '1px solid rgba(251, 191, 36, 0.4)', borderRadius: '999px', padding: '2px 8px', marginRight: '8px' }}>edited by you</span>
        )}
        <div className={`tool-status-pill tool-status-${needsApproval ? 'approval' : (status || 'pending')}`} style={needsApproval ? { background: 'rgba(239, 68, 68, 0.2)', color: '#fca5a5', border: '1px solid rgba(239, 68, 68, 0.4)' } : {}}>
          {needsApproval ? <ShieldAlert size={11} color="#ef4444" /> : isPending ? <span className="tool-spinner-dot" /> : isFailed ? <AlertCircle size={11} /> : <CheckCircle2 size={11} />}
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
            background: 'rgba(239, 68, 68, 0.12)',
            border: '1px solid rgba(239, 68, 68, 0.35)',
            borderRadius: '10px',
            marginTop: '8px',
            display: 'flex',
            flexDirection: 'column',
            gap: '10px'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#fca5a5', fontSize: '13px', fontWeight: 600 }}>
              <ShieldAlert size={16} color="#ef4444" />
              <span>Execution Approval Required</span>
            </div>
            <div style={{ fontSize: '12px', color: '#cbd5e1' }}>
              The agent is requesting permission to execute <strong>{tool_name}</strong>.
            </div>
            <ApprovalPreview preview={approvalPreview} />
            {!argsEditorOpen ? (
              <button
                data-testid="approval-edit-args-btn"
                onClick={openArgsEditor}
                style={{ alignSelf: 'flex-start', padding: '6px 12px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.08)', color: '#e2e8f0', border: '1px solid rgba(255, 255, 255, 0.15)', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}>
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
                  style={{ width: '100%', boxSizing: 'border-box', background: 'rgba(0,0,0,0.5)', color: '#a78bfa', border: '1px solid rgba(255, 255, 255, 0.15)', borderRadius: '8px', padding: '10px', fontFamily: 'monospace', fontSize: '12px' }}
                />
                {argsError && (
                  <div data-testid="approval-args-error" style={{ fontSize: '12px', color: '#fca5a5' }}>{argsError}</div>
                )}
              </div>
            )}
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'flex-end', marginTop: '4px' }}>
              <button 
                onClick={() => onApproveTool && onApproveTool(pendingApproval.id, false, pendingApproval.approval_key)}
                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  background: 'rgba(255, 255, 255, 0.08)',
                  color: '#e2e8f0',
                  border: '1px solid rgba(255, 255, 255, 0.15)',
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

                    style={{
                      padding: '6px 14px',
                      borderRadius: '8px',
                      background: 'rgba(255, 255, 255, 0.08)',
                      color: '#e2e8f0',
                      border: '1px solid rgba(255, 255, 255, 0.15)',
                      cursor: 'pointer',
                      fontSize: '12px',
                      fontWeight: 500
                    }}
                  >
                    Approve + remember file
                  </button>
                  <button
                    onClick={() => approveWithEdits('folder')}

                    style={{
                      padding: '6px 14px',
                      borderRadius: '8px',
                      background: 'rgba(255, 255, 255, 0.08)',
                      color: '#e2e8f0',
                      border: '1px solid rgba(255, 255, 255, 0.15)',
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

                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  background: '#ef4444',
                  color: '#fff',
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

  const { from_node, to_node, action, reason, briefing, input_payload, output_payload, status } = activity;

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
      </div>

      {(reason || briefing) && (
        <div className="node-intent-banner">
          {reason && <div className="node-intent-reason"><strong style={{ color: '#c4b5fd' }}>Goal:</strong> {reason}</div>}
          {briefing && <div className="node-intent-briefing"><strong style={{ color: '#93c5fd' }}>Briefing:</strong> {briefing}</div>}
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
              <TerminalSquare size={12} color="#fbbf24" />
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

export function ThinkingAccordion({ thoughts = [], isThinking, hasError, onStopTask, onApproveTool = null, pendingApprovals = [], onOpenArtifact = null }) {
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
      // Filter out redundant supervisor delegation texts or echoed assistant conversational outputs
      if (!text || text.startsWith('[Supervisor] -> Delegating') || text.startsWith('Hello! The current time')) {
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

  if (treeNodes.length === 0 && !isThinking && !hasError) return null;

  return (
    <div style={{
      marginBottom: '12px',
      borderRadius: '14px',
      background: 'rgba(10, 10, 20, 0.45)',
      border: isExpanded ? '1px solid rgba(167, 139, 250, 0.3)' : '1px solid rgba(255, 255, 255, 0.08)',
      overflow: 'hidden',
      transition: 'all 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
      boxShadow: isExpanded ? '0 8px 24px rgba(0, 0, 0, 0.4)' : 'none'
    }}>
      <div 
        onClick={() => setIsExpanded(!isExpanded)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '9px 13px',
          background: isExpanded ? 'rgba(255, 255, 255, 0.04)' : 'transparent',
          color: isThinking ? '#c4b5fd' : '#9090a0',
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
            border: '2px solid rgba(167, 139, 250, 0.3)',
            borderTopColor: '#a78bfa', 
            borderRadius: '50%',
            animation: 'spin 1s linear infinite',
            flexShrink: 0
          }} />
        ) : hasError ? (
          <X size={13} color="#ef4444" style={{ flexShrink: 0 }} />
        ) : (
          <CheckCircle2 size={13} color="#10b981" style={{ flexShrink: 0 }} />
        )}
        
        <span style={{ flex: 1, textAlign: 'left', fontWeight: '500', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span>{isThinking ? 'Agent is thinking...' : hasError ? 'Task Stopped' : `Execution Pipeline (${treeNodes.length} steps)`}</span>
          <span style={{ fontSize: '10.5px', opacity: 0.65, fontWeight: 400, color: '#a78bfa' }}>
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
            color: '#d4d4d8',
            maxHeight: '380px',
            overflowY: 'auto',
            borderTop: '1px solid rgba(255, 255, 255, 0.06)',
            background: 'rgba(0, 0, 0, 0.25)'
          }} 
          className="chat-thought-container chat-scroll"
        >
          {(!treeNodes || treeNodes.length === 0) ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', padding: '6px 0', color: '#a1a1aa' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: hasError ? '#ef4444' : '#c4b5fd' }}>
                {!hasError && <span className="dot-typing" style={{ width: '5px', height: '5px', display: 'inline-block' }}></span>}
                <span>{hasError ? 'Task was stopped before supervisor node could initialize.' : 'Initializing supervisor node & analyzing prompt requirements...'}</span>
              </div>
              <div style={{ fontSize: '11px', color: '#71717a' }}>
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
                const approval = pendingApprovals.find(a => a.tool_name === item.payload?.tool_name);
                return (
                  <ToolActivityCard 
                    key={item.key} 
                    payload={item.payload} 
                    pendingApproval={approval}
                    onApproveTool={onApproveTool}
                    onOpenArtifact={onOpenArtifact}
                  />
                );
              }
              // Text item
              const nodeStyle = getNodeStyle(item.node);
              return (
                <div key={item.key} style={{ marginBottom: '10px', padding: '8px 10px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.02)', border: '1px solid rgba(255, 255, 255, 0.04)' }}>
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

// Epic 1, E1-17: display-only scratchpad panel. Mid-task the agent
// can sketch a diagram / SVG / notes here; the content arrives
// pre-sanitised from the engine (scripts and handlers stripped). The
// panel sits above the composer so it never interrupts the message
// stream; it collapses, and dismissing hides it until the next
// SCRATCHPAD event (App stores a fresh object per event, which the
// effect below treats as "reopen").
export function ScratchpadPanel({ scratchpad }) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [isDismissed, setIsDismissed] = useState(false);
  const [mermaidSvg, setMermaidSvg] = useState('');
  const [mermaidError, setMermaidError] = useState('');

  const kind = scratchpad?.kind || 'text';
  const content = typeof scratchpad?.content === 'string' ? scratchpad.content : '';
  const agent = scratchpad?.agent || null;

  useEffect(() => {
    setIsDismissed(false);
    setIsCollapsed(false);
  }, [scratchpad]);

  // Mermaid sources render to inline SVG with the package loaded
  // lazily (importing this window never pays for it). Strict security
  // level plus the engine-side sanitisation keep it display-only.
  // Any failure falls back to showing the source with a note — the
  // panel must never take the chat down with it.
  useEffect(() => {
    if (!scratchpad || kind !== 'mermaid') {
      setMermaidSvg('');
      setMermaidError('');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const mod = await import('mermaid');
        const mermaid = mod.default || mod;
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
        const renderId = `aether-scratchpad-${Math.random().toString(36).slice(2, 10)}`;
        const result = await mermaid.render(renderId, content);
        if (!cancelled) {
          setMermaidSvg(result?.svg || '');
          setMermaidError('');
        }
      } catch (err) {
        if (!cancelled) {
          setMermaidSvg('');
          setMermaidError(err?.message || 'render failed');
        }
      }
    })();
    return () => { cancelled = true; };
  }, [scratchpad, kind, content]);

  if (!scratchpad || isDismissed) return null;

  return (
    <div
      className="scratchpad-panel"
      data-testid="scratchpad-panel"
      style={{
        margin: '12px 16px 0 16px',
        borderRadius: '14px',
        background: 'rgba(10, 14, 24, 0.55)',
        border: '1px solid rgba(96, 165, 250, 0.28)',
        overflow: 'hidden',
        flexShrink: 0
      }}
    >
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        padding: '8px 12px',
        color: '#93c5fd',
        fontSize: '12px',
        userSelect: 'none'
      }}>
        <Edit2 size={13} style={{ flexShrink: 0 }} />
        <span style={{ fontWeight: 600, letterSpacing: '0.4px' }}>Scratchpad</span>
        <span style={{
          fontSize: '10px',
          background: 'rgba(96, 165, 250, 0.16)',
          border: '1px solid rgba(96, 165, 250, 0.35)',
          padding: '2px 6px',
          borderRadius: '4px',
          fontWeight: 600,
          letterSpacing: '0.5px',
          textTransform: 'uppercase'
        }}>{kind}</span>
        {agent && <span style={{ fontSize: '11px', color: '#a1a1aa' }}>{agent}</span>}
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={() => setIsCollapsed(!isCollapsed)}
          aria-label={isCollapsed ? 'Expand scratchpad' : 'Collapse scratchpad'}
          title={isCollapsed ? 'Expand scratchpad' : 'Collapse scratchpad'}
          style={{ background: 'transparent', border: 'none', color: '#93c5fd', cursor: 'pointer', display: 'flex', alignItems: 'center', padding: '2px' }}
        >
          <ChevronDown size={14} style={{ transition: 'transform 0.3s ease', transform: isCollapsed ? 'rotate(0deg)' : 'rotate(180deg)' }} />
        </button>
        <button
          type="button"
          onClick={() => setIsDismissed(true)}
          aria-label="Dismiss scratchpad"
          title="Dismiss scratchpad"
          style={{ background: 'transparent', border: 'none', color: '#a1a1aa', cursor: 'pointer', display: 'flex', alignItems: 'center', padding: '2px' }}
        >
          <X size={14} />
        </button>
      </div>
      {!isCollapsed && (
        <div
          className="chat-scroll"
          style={{
            maxHeight: '260px',
            overflowY: 'auto',
            overflowX: 'auto',
            padding: '10px 12px',
            borderTop: '1px solid rgba(255, 255, 255, 0.06)',
            background: 'rgba(0, 0, 0, 0.25)'
          }}
        >
          {kind === 'svg' && (
            <div className="scratchpad-svg" dangerouslySetInnerHTML={{ __html: content }} />
          )}
          {kind === 'mermaid' && (mermaidSvg ? (
            <div className="scratchpad-mermaid" dangerouslySetInnerHTML={{ __html: mermaidSvg }} />
          ) : mermaidError ? (
            <div>
              <div style={{ fontSize: '11px', color: '#fca5a5', marginBottom: '6px' }}>
                Diagram could not be rendered — showing the source instead.
              </div>
              <pre className="scratchpad-source" style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '11.5px', color: '#d4d4d8' }}>{content}</pre>
            </div>
          ) : (
            <div style={{ fontSize: '11.5px', color: '#a1a1aa' }}>Rendering diagram…</div>
          ))}
          {kind !== 'svg' && kind !== 'mermaid' && (
            <pre className="scratchpad-text" style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '12px', color: '#e2e8f0' }}>{content}</pre>
          )}
        </div>
      )}
    </div>
  );
}

function ContextMenu({ menu, onClose, onShowToast, setInput }) {
  if (!menu) return null;

  const handleCopySelection = async (e) => {
    e.stopPropagation();
    if (menu.selectedText) {
      try {
        await navigator.clipboard.writeText(menu.selectedText);
        onShowToast('Copied selection to clipboard');
      } catch (err) {
        console.error(err);
      }
    }
    onClose();
  };

  const handleCopyFull = async (e) => {
    e.stopPropagation();
    if (menu.fullText) {
      try {
        await navigator.clipboard.writeText(menu.fullText);
        onShowToast(menu.isThought ? 'Copied thought to clipboard' : 'Copied message to clipboard');
      } catch (err) {
        console.error(err);
      }
    }
    onClose();
  };

  const handleCut = async (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      const el = menu.inputElement;
      const start = el.selectionStart || 0;
      const end = el.selectionEnd || 0;
      const val = el.value || '';
      const cutText = val.slice(start, end);
      if (cutText) {
        try {
          await navigator.clipboard.writeText(cutText);
          const nextVal = val.slice(0, start) + val.slice(end);
          setInput(nextVal);
          onShowToast('Cut to clipboard');
          setTimeout(() => {
            el.selectionStart = el.selectionEnd = start;
            el.focus();
          }, 0);
        } catch (err) {
          console.error(err);
        }
      }
    }
    onClose();
  };

  const handlePaste = async (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      const el = menu.inputElement;
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          const start = el.selectionStart || 0;
          const end = el.selectionEnd || 0;
          const val = el.value || '';
          const nextVal = val.slice(0, start) + text + val.slice(end);
          setInput(nextVal);
          onShowToast('Pasted from clipboard');
          setTimeout(() => {
            el.selectionStart = el.selectionEnd = start + text.length;
            el.focus();
          }, 0);
        }
      } catch (err) {
        console.error('Clipboard paste failed:', err);
      }
    }
    onClose();
  };

  const handleSelectAll = (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      menu.inputElement.select();
      menu.inputElement.focus();
    } else if (menu.targetElement) {
      const range = document.createRange();
      range.selectNodeContents(menu.targetElement);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    onClose();
  };

  return (
    <div 
      className="ios-context-menu"
      style={{ left: `${menu.x}px`, top: `${menu.y}px` }}
      onClick={(e) => e.stopPropagation()}
    >
      {menu.isInput ? (
        <>
          <div 
            className={`ios-context-item ${!menu.selectedText ? 'disabled' : ''}`}
            onClick={handleCut}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Scissors size={14} color="#a78bfa" />
              <span>Cut</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+X</span>
          </div>

          <div 
            className={`ios-context-item ${!menu.selectedText ? 'disabled' : ''}`}
            onClick={handleCopySelection}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Copy size={14} color="#a78bfa" />
              <span>Copy</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+C</span>
          </div>

          <div className="ios-context-item" onClick={handlePaste}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Clipboard size={14} color="#a78bfa" />
              <span>Paste</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+V</span>
          </div>

          <div className="ios-context-divider" />

          <div className="ios-context-item" onClick={handleSelectAll}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <CheckCheck size={14} color="#a78bfa" />
              <span>Select All</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+A</span>
          </div>
        </>
      ) : (
        <>
          {menu.selectedText ? (
            <div className="ios-context-item" onClick={handleCopySelection}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Copy size={14} color="#a78bfa" />
                <span>Copy Selection</span>
              </div>
              <span className="ios-context-shortcut">Ctrl+C</span>
            </div>
          ) : null}

          {menu.fullText ? (
            <div className="ios-context-item" onClick={handleCopyFull}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Copy size={14} color="#38bdf8" />
                <span>{menu.isThought ? 'Copy Thought' : 'Copy Message'}</span>
              </div>
            </div>
          ) : null}

          <div className="ios-context-divider" />

          <div className="ios-context-item" onClick={handleSelectAll}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <CheckCheck size={14} color="#a78bfa" />
              <span>Select All</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+A</span>
          </div>
        </>
      )}
    </div>
  );
}

// Epic 8, E8-6.1: browser task card for the dedicated agent browser
export function BrowserTaskCard({ session = {}, onControl = () => {} }) {
  const { controlState = 'agent', currentUrl = '', challenge = null } = session || {};

  const isStopped = controlState === 'stopped';
  const isPaused = controlState === 'paused';
  const isUserControl = controlState === 'user';
  const isWaitingUser = controlState === 'waiting_user';

  const challengeType = challenge?.challenge ? String(challenge.challenge).toLowerCase() : '';
  const challengeDisplay = challengeType === 'two_factor' ? 'Two-factor' : (challengeType || 'verification');

  return (
    <div
      data-testid="browser-task-card"
      style={{
        margin: '12px 16px 0 16px',
        borderRadius: '14px',
        border: '1px solid rgba(59, 130, 246, 0.25)',
        background: 'rgba(15, 23, 42, 0.85)',
        backdropFilter: 'blur(16px)',
        padding: '12px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
        boxShadow: '0 4px 20px rgba(0, 0, 0, 0.25)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
          <div style={{
            width: '28px',
            height: '28px',
            borderRadius: '8px',
            background: 'rgba(59, 130, 246, 0.15)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#60a5fa',
            flexShrink: 0,
          }}>
            <Globe size={16} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            <span style={{ fontSize: '13px', fontWeight: 600, color: '#f1f5f9' }}>
              Agent Browser (Dedicated Profile)
            </span>
            {currentUrl ? (
              <span
                style={{
                  fontSize: '11px',
                  color: '#94a3b8',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={currentUrl}
              >
                {currentUrl}
              </span>
            ) : null}
          </div>
        </div>

        <div style={{
          fontSize: '11px',
          fontWeight: 500,
          padding: '2px 8px',
          borderRadius: '999px',
          background: isWaitingUser
            ? 'rgba(245, 158, 11, 0.15)'
            : isUserControl
            ? 'rgba(168, 85, 247, 0.15)'
            : isPaused
            ? 'rgba(234, 179, 8, 0.15)'
            : isStopped
            ? 'rgba(239, 68, 68, 0.15)'
            : 'rgba(59, 130, 246, 0.15)',
          color: isWaitingUser
            ? '#fbbf24'
            : isUserControl
            ? '#c084fc'
            : isPaused
            ? '#fde047'
            : isStopped
            ? '#f87171'
            : '#60a5fa',
          border: `1px solid ${
            isWaitingUser
              ? 'rgba(245, 158, 11, 0.3)'
              : isUserControl
              ? 'rgba(168, 85, 247, 0.3)'
              : isPaused
              ? 'rgba(234, 179, 8, 0.3)'
              : isStopped
              ? 'rgba(239, 68, 68, 0.3)'
              : 'rgba(59, 130, 246, 0.3)'
          }`,
        }}>
          {isWaitingUser
            ? 'Parked'
            : isUserControl
            ? 'User in control'
            : isPaused
            ? 'Paused'
            : isStopped
            ? 'Stopped'
            : 'Active'}
        </div>
      </div>

      {isWaitingUser && (
        <div style={{
          background: 'rgba(245, 158, 11, 0.1)',
          border: '1px solid rgba(245, 158, 11, 0.25)',
          borderRadius: '8px',
          padding: '8px 12px',
          fontSize: '12px',
          color: '#fef3c7',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
        }}>
          <ShieldAlert size={16} color="#fbbf24" style={{ flexShrink: 0 }} />
          <span>
            Waiting for you: {challengeDisplay} challenge detected in browser window. The agent never solves challenges. Please complete it to continue.
          </span>
        </div>
      )}

      {isUserControl && (
        <div style={{
          background: 'rgba(168, 85, 247, 0.1)',
          border: '1px solid rgba(168, 85, 247, 0.25)',
          borderRadius: '8px',
          padding: '8px 12px',
          fontSize: '12px',
          color: '#f3e8ff',
        }}>
          You have control of the browser window. The agent is waiting for you to return control.
        </div>
      )}

      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
        {isPaused ? (
          <button
            type="button"
            data-testid="browser-resume-btn"
            onClick={() => onControl('resume')}
            style={{
              background: 'rgba(59, 130, 246, 0.2)',
              border: '1px solid rgba(59, 130, 246, 0.4)',
              color: '#93c5fd',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Play size={13} />
            <span>Resume</span>
          </button>
        ) : !isWaitingUser && !isUserControl ? (
          <button
            type="button"
            data-testid="browser-pause-btn"
            disabled={isStopped}
            onClick={() => onControl('pause')}
            style={{
              background: isStopped ? 'rgba(255, 255, 255, 0.05)' : 'rgba(234, 179, 8, 0.15)',
              border: isStopped ? '1px solid rgba(255, 255, 255, 0.1)' : '1px solid rgba(234, 179, 8, 0.35)',
              color: isStopped ? '#64748b' : '#fde047',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: isStopped ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Pause size={13} />
            <span>Pause</span>
          </button>
        ) : null}

        {isUserControl ? (
          <button
            type="button"
            data-testid="browser-return-control-btn"
            onClick={() => onControl('return_control')}
            style={{
              background: 'rgba(168, 85, 247, 0.2)',
              border: '1px solid rgba(168, 85, 247, 0.4)',
              color: '#d8b4fe',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Bot size={13} />
            <span>Return Control</span>
          </button>
        ) : !isWaitingUser ? (
          <button
            type="button"
            data-testid="browser-take-control-btn"
            disabled={isStopped}
            onClick={() => onControl('take_control')}
            style={{
              background: isStopped ? 'rgba(255, 255, 255, 0.05)' : 'rgba(168, 85, 247, 0.15)',
              border: isStopped ? '1px solid rgba(255, 255, 255, 0.1)' : '1px solid rgba(168, 85, 247, 0.35)',
              color: isStopped ? '#64748b' : '#d8b4fe',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: isStopped ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <User size={13} />
            <span>Take Control</span>
          </button>
        ) : null}

        {isWaitingUser && (
          <button
            type="button"
            data-testid="browser-resume-btn"
            onClick={() => onControl('resume')}
            style={{
              background: 'rgba(59, 130, 246, 0.2)',
              border: '1px solid rgba(59, 130, 246, 0.4)',
              color: '#93c5fd',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Play size={13} />
            <span>Resume</span>
          </button>
        )}

        <button
          type="button"
          data-testid="browser-stop-btn"
          disabled={isStopped}
          onClick={() => onControl('stop')}
          style={{
            background: isStopped ? 'rgba(255, 255, 255, 0.05)' : 'rgba(239, 68, 68, 0.15)',
            border: isStopped ? '1px solid rgba(255, 255, 255, 0.1)' : '1px solid rgba(239, 68, 68, 0.35)',
            color: isStopped ? '#64748b' : '#fca5a5',
            borderRadius: '8px',
            padding: '5px 12px',
            fontSize: '12px',
            fontWeight: 500,
            cursor: isStopped ? 'not-allowed' : 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}
        >
          <Square size={13} />
          <span>Stop</span>
        </button>
      </div>
    </div>
  );
}


export default function DraggableChatWindow({ 
  messages, 
  onSendMessage, 
  onStopTask,
  status, 
  isThinking,
  onApproveTool,
  onApprovePlugin,
  providers = [],
  onChangeModel = () => {},
  onEditMessage,
  onRegenerate,
  planMode = false,
  scratchpad = null,
  onTogglePlanMode,
  onDetach,
  isFloating,
  initialInput = '',
  slashCommands = [],
  isSidebarCollapsed = false,
  onExportSession = () => {},
  sessionId = null,
  rewindManifest = null,
  onRewindRequest = null,
  onForkRequest = null,
  rewindUndo = null,
  onRewindUndo = null,
  onRewindUndoExpire = null,
  artifactsCount = 0,
  isCanvasOpen = false,
  onToggleCanvas = () => {},
  onOpenArtifact = () => {},
  onTriggerSnip,
  initialAttachments = [],
  sessionCostUsd = 0,
  costTasks = [],
  runningCostTaskIds = [],
  onRequestCostBreakdown = () => {},
  browserSession = null,
  onBrowserControl = null,
  voiceState = null,
  voiceAudioChunk = null,
  voicePlaybackCut = 0,
  onVoiceStart = () => {},
  onVoiceStop = () => {},
  onVoiceDismissError = () => {}
}) {
  const [input, setInput] = useState(initialInput);
  const [isMinimized, setIsMinimized] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const exportMenuRef = useRef(null);
  const [showCostPopover, setShowCostPopover] = useState(false);
  const costMenuRef = useRef(null);
  const [contextMenu, setContextMenu] = useState(null);
  const [toastMessage, setToastMessage] = useState('');  // Epic 8, E8-1.3: per-approval argument editor state for the
  // inline approval cards, keyed by message id: { text, error }.
  // Presence of an entry means that card's editor is open.
  const [approvalArgEdits, setApprovalArgEdits] = useState({});
  // Epic 8, E8-1.1a: which message's step menu is open (index into
  // messages), or null. One menu at a time.
  const [openStepMenu, setOpenStepMenu] = useState(null);
  const [attachments, setAttachments] = useState(initialAttachments || []);
  const [isSnipOverlayOpen, setIsSnipOverlayOpen] = useState(false);

  // Epic 8, E8-6.1: agent-browser control and activity tracking
  const [localControlState, setLocalControlState] = useState(null);
  const lastBrowserStateRef = useRef(browserSession?.controlState);
  const lastBrowserChallengeRef = useRef(browserSession?.challenge);

  if (lastBrowserStateRef.current !== browserSession?.controlState ||
      lastBrowserChallengeRef.current !== browserSession?.challenge) {
    lastBrowserStateRef.current = browserSession?.controlState;
    lastBrowserChallengeRef.current = browserSession?.challenge;
    if (localControlState !== null) {
      setLocalControlState(null);
    }
  }

  const browserActivity = React.useMemo(() => {
    let latest = null;
    for (const m of messages || []) {
      for (const t of m.thoughts || []) {
        const payload = t.payload || {};
        const toolName = payload.tool_name || t.tool_name || '';
        if (typeof toolName === 'string' && toolName.startsWith('browser_')) {
          const url = payload.tool_args?.url || payload.args?.url || '';
          latest = { toolName, url };
        }
      }
    }
    return latest;
  }, [messages]);

  const activeBrowserSession = React.useMemo(() => {
    if (!browserActivity && !browserSession) return null;
    return {
      controlState: localControlState || browserSession?.controlState || 'agent',
      currentUrl: browserSession?.currentUrl || browserActivity?.url || '',
      challenge: browserSession?.challenge || null,
    };
  }, [browserActivity, browserSession, localControlState]);

  const handleBrowserControlClick = React.useCallback((action) => {
    if (action === 'pause') {
      setLocalControlState('paused');
    } else if (action === 'resume') {
      setLocalControlState('agent');
    } else if (action === 'take_control') {
      setLocalControlState('user');
    } else if (action === 'return_control') {
      setLocalControlState('agent');
    } else if (action === 'stop') {
      setLocalControlState('stopped');
    }
    if (onBrowserControl) {
      onBrowserControl(action);
    }
  }, [onBrowserControl]);


  useEffect(() => {
    if (initialAttachments && initialAttachments.length > 0) {
      setAttachments(initialAttachments);
    }
  }, [initialAttachments]);

  useEffect(() => {
    if (window.electronAPI && typeof window.electronAPI.onTriggerSnip === 'function') {
      return window.electronAPI.onTriggerSnip(() => {
        setIsSnipOverlayOpen(true);
      });
    }
  }, []);

  const handleTriggerSnip = () => {
    if (onTriggerSnip) {
      onTriggerSnip();
    }
    setIsSnipOverlayOpen(true);
  };

  const handleSnipCaptured = (snip) => {
    setIsSnipOverlayOpen(false);
    setAttachments(prev => [...prev, snip]);
  };

  const handleRemoveAttachment = (idxToRemove) => {
    setAttachments(prev => prev.filter((_, idx) => idx !== idxToRemove));
  };

  const approveToolWithEdits = (msg, scope) => {
    if (!onApproveTool) return;
    const editState = approvalArgEdits[msg.id];
    if (!editState) {
      if (scope) onApproveTool(msg.id, true, msg.approval_key, scope);
      else onApproveTool(msg.id, true, msg.approval_key);
      return;
    }
    const { editedArgs, error } = parseApprovalEditedArgs(editState.text, msg.tool_args || {});
    if (error) {
      setApprovalArgEdits(prev => ({ ...prev, [msg.id]: { ...editState, error } }));
      return;
    }
    if (editedArgs) onApproveTool(msg.id, true, msg.approval_key, scope, editedArgs);
    else if (scope) onApproveTool(msg.id, true, msg.approval_key, scope);
    else onApproveTool(msg.id, true, msg.approval_key);
  };

  const toastTimeoutRef = useRef(null);
  const messagesEndRef = useRef(null);
  const nodeRef = useRef(null);
  const textareaRef = useRef(null);
  const slashPopupRef = useRef(null);

  const [showSlashPopup, setShowSlashPopup] = useState(false);
  const [slashFilter, setSlashFilter] = useState('');
  const [slashIndex, setSlashIndex] = useState(0);
  const [filteredCommands, setFilteredCommands] = useState([]);

  const showToast = (msg) => {
    setToastMessage(msg);
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    toastTimeoutRef.current = setTimeout(() => {
      setToastMessage('');
    }, 2000);
  };

  useEffect(() => {
    const handleGlobalClick = (e) => {
      setContextMenu(null);
      setOpenStepMenu(null);
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target)) {
        setShowExportMenu(false);
      if (costMenuRef.current && !costMenuRef.current.contains(e.target)) {
        setShowCostPopover(false);
      }
      }
    };
    const handleGlobalKeyDown = (e) => {
      if (e.key === 'Escape') {
        setContextMenu(null);
        setShowExportMenu(false);
        setShowCostPopover(false);
      }
    };
    window.addEventListener('click', handleGlobalClick);
    window.addEventListener('scroll', handleGlobalClick, true);
    window.addEventListener('keydown', handleGlobalKeyDown);
    return () => {
      window.removeEventListener('click', handleGlobalClick);
      window.removeEventListener('scroll', handleGlobalClick, true);
      window.removeEventListener('keydown', handleGlobalKeyDown);
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    if (showSlashPopup && slashPopupRef.current) {
      const selectedEl = slashPopupRef.current.children[slashIndex];
      if (selectedEl) {
        selectedEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    }
  }, [slashIndex, showSlashPopup]);

  const handleInputChange = (e) => {
    const val = e.target.value;
    setInput(val);
    
    if (val.startsWith('/')) {
      const parts = val.split(' ');
      if (parts.length === 1) {
        const filterStr = parts[0].toLowerCase();
        const filtered = slashCommands.filter(c => 
          c.command.toLowerCase().includes(filterStr) || 
          c.description.toLowerCase().includes(filterStr.substring(1))
        );
        setFilteredCommands(filtered);
        setSlashIndex(0);
        setShowSlashPopup(true);
        setSlashFilter(filterStr);
      } else {
        setShowSlashPopup(false);
      }
    } else {
      setShowSlashPopup(false);
    }
  };

  const handleInputKeyDown = (e) => {
    if (showSlashPopup && filteredCommands.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashIndex(prev => (prev < filteredCommands.length - 1 ? prev + 1 : 0));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex(prev => (prev > 0 ? prev - 1 : filteredCommands.length - 1));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const selected = filteredCommands[slashIndex];
        if (selected) {
          setInput(selected.command + ' ');
        }
        setShowSlashPopup(false);
        return;
      }
      if (e.key === 'Escape') {
        setShowSlashPopup(false);
        return;
      }
    }
    
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleContextMenu = (e) => {
    const target = e.target;
    const isInput = target.tagName === 'TEXTAREA' || target.tagName === 'INPUT';
    
    const selection = window.getSelection();
    const selectedText = selection ? selection.toString().trim() : '';

    const thoughtEl = target.closest('.thought-step-content') || target.closest('.chat-thought-container');
    const bubbleEl = target.closest('.chat-bubble');
    
    let fullText = '';
    let isThought = false;
    if (thoughtEl) {
      fullText = thoughtEl.innerText || '';
      isThought = true;
    } else if (bubbleEl) {
      fullText = bubbleEl.innerText || '';
    }

    if (!selectedText && !isInput && !fullText) {
      setContextMenu(null);
      return;
    }

    e.preventDefault();

    const menuWidth = 190;
    const menuHeight = isInput ? 180 : 130;
    let x = e.clientX;
    let y = e.clientY;
    
    let maxX = window.innerWidth;
    let maxY = window.innerHeight;

    if (nodeRef.current) {
      const rect = nodeRef.current.getBoundingClientRect();
      x -= rect.left;
      y -= rect.top;
      maxX = rect.width;
      maxY = rect.height;
    }

    if (x + menuWidth > maxX - 10) {
      x = maxX - menuWidth - 10;
    }
    if (y + menuHeight > maxY - 10) {
      y = maxY - menuHeight - 10;
    }

    setContextMenu({
      x,
      y,
      isInput,
      selectedText,
      fullText,
      isThought,
      inputElement: isInput ? target : null,
      targetElement: thoughtEl || bubbleEl || target
    });
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    if (!isMinimized) {
      scrollToBottom();
    }
  }, [messages, isThinking, isMinimized]);

  const handleSubmit = (e) => {
    e.preventDefault();
    // While a task runs, submitting steers it (Epic 1, E1-07) - App
    // decides START_TASK vs STEER_TASK; the input must not dead-end.
    if (input.trim() || attachments.length > 0) {
      if (attachments.length > 0) {
        onSendMessage(input.trim(), attachments);
      } else {
        onSendMessage(input.trim());
      }
      setInput('');
      setAttachments([]);
    }
  };

  // Epic 1 UI batch (E1-08): regenerate is offered on the last final
  // agent bubble only (error bubbles included), and never mid-task.
  const lastFinalAgentIndex = messages.findLastIndex(
    m => (m.role === 'agent' || m.role === 'assistant') && m.isFinal
  );

  const handleMouseMove = (e) => {
    if (nodeRef.current) {
      const rect = nodeRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      nodeRef.current.style.setProperty('--mouse-x', `${x}px`);
      nodeRef.current.style.setProperty('--mouse-y', `${y}px`);
    }
  };

  return (
    <div 
      ref={nodeRef}
      className="ios-glass"
      onMouseMove={handleMouseMove}
      onContextMenu={handleContextMenu}
      style={isFloating ? {
        position: 'relative',
        margin: '0',
        width: '100vw',
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
        boxShadow: 'inset 0 1px 1px rgba(255, 255, 255, 0.15), inset 0 0 40px rgba(124, 58, 237, 0.05), inset 0 -1px 1px rgba(0, 0, 0, 0.4)',
        transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
        opacity: isMinimized ? 0.8 : 1
      } : {
        position: 'absolute',
        top: '40px',
        left: isSidebarCollapsed ? '80px' : '360px',
        right: isCanvasOpen ? '530px' : '40px',
        bottom: '40px',
        width: 'auto',
        height: 'auto',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
        transition: 'all 250ms cubic-bezier(0.23, 1, 0.32, 1)'
      }}
    >
      {/* Header */}
      <div 
        className="title-bar"
        style={{
          padding: '12px 16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid rgba(255,255,255,0.08)',
          background: 'rgba(255,255,255,0.03)',
          WebkitAppRegion: isFloating ? 'drag' : undefined
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1 }}>
          {isFloating && <GripHorizontal size={16} color="rgba(255,255,255,0.4)" />}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1 }}>
              <div style={{ 
                width: '8px', 
                height: '8px', 
                borderRadius: '50%', 
                background: status.includes('Online') ? '#10b981' : '#ef4444',
                boxShadow: status.includes('Online') ? '0 0 8px #10b981' : '0 0 8px #ef4444',
                flexShrink: 0
              }}></div>
              
              <Dropdown 
                value={
                  (() => {
                    const activeProvider = (Array.isArray(providers) ? providers : []).find(p => p.is_default);
                    return activeProvider && activeProvider.default_model ? `${activeProvider.name}:::${activeProvider.default_model}` : '';
                  })()
                }
                onChange={(val) => {
                  const [provider, model] = val.split(':::');
                  if (provider && model) {
                    onChangeModel(provider, model);
                  }
                }}
                options={
                  (Array.isArray(providers) ? providers : [])
                    .filter(p => p.has_key && p.models && p.models.length > 0)
                    .map(p => ({
                      group: p.display_name || p.name,
                      items: p.models.map(m => ({ value: `${p.name}:::${m}`, label: m }))
                    }))
                }
                placeholder="Select Model..."
                style={{ maxWidth: '240px' }}
              />

            </div>
          </div>
          <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
            {/* Cost Ledger (Epic 8, E8-A8.1a) */}
            <div ref={costMenuRef} style={{ position: 'relative' }}>
              <button
                type="button"
                data-testid="cost-header-btn"
                onClick={() => {
                  setShowCostPopover(prev => !prev);
                  if (!showCostPopover) onRequestCostBreakdown();
                }}
                title="Session cost"
                aria-label="Session cost"
                style={{
                  background: showCostPopover ? 'rgba(167, 139, 250, 0.25)' : 'rgba(255, 255, 255, 0.1)',
                  border: showCostPopover ? '1px solid rgba(167, 139, 250, 0.5)' : '1px solid rgba(255, 255, 255, 0.1)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px 10px',
                  borderRadius: '999px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.2s',
                  fontSize: '12px',
                  fontWeight: 500
                }}
              >
                <CircleDollarSign size={14} />
                <span data-testid="cost-header-total">{formatUsd(sessionCostUsd)}</span>
              </button>
              {showCostPopover && (
                <CostHeaderPopover
                  tasks={costTasks}
                  totalCostUsd={sessionCostUsd}
                  runningTaskIds={runningCostTaskIds}
                />
              )}
            </div>
            {/* Artifacts Canvas Toggle (Epic 8, E8-2.1) */}
            <button
              type="button"
              data-testid="artifacts-header-btn"
              onClick={() => onToggleCanvas && onToggleCanvas()}
              title="Artifacts"
              aria-label="Toggle Artifacts Canvas"
              style={{
                background: isCanvasOpen ? 'rgba(167, 139, 250, 0.25)' : 'rgba(255, 255, 255, 0.1)',
                border: isCanvasOpen ? '1px solid rgba(167, 139, 250, 0.5)' : '1px solid rgba(255, 255, 255, 0.1)',
                color: '#fff',
                cursor: 'pointer',
                padding: '6px 10px',
                borderRadius: '999px',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                transition: 'all 0.2s',
                fontSize: '12px',
                fontWeight: 500
              }}
            >
              <Layers size={14} />
              <span>Artifacts</span>
              <span
                data-testid="artifacts-badge"
                style={{
                  background: isCanvasOpen ? '#8b5cf6' : 'rgba(255, 255, 255, 0.15)',
                  color: '#fff',
                  borderRadius: '999px',
                  padding: '1px 6px',
                  fontSize: '10px',
                  fontWeight: 600,
                  minWidth: '16px',
                  textAlign: 'center'
                }}
              >
                {artifactsCount}
              </span>
            </button>
            {/* Voice mode (Epic 8, E8-5.1a) */}
            <button
              type="button"
              data-testid="voice-header-btn"
              onClick={() => {
                if (voiceState && (voiceState.status === 'active' || voiceState.status === 'starting')) onVoiceStop();
                else onVoiceStart();
              }}
              title="Voice mode"
              aria-label="Toggle voice mode"
              style={{
                background: voiceState && voiceState.status === 'active' ? 'rgba(167, 139, 250, 0.25)' : 'rgba(255, 255, 255, 0.1)',
                border: voiceState && voiceState.status === 'active' ? '1px solid rgba(167, 139, 250, 0.5)' : '1px solid rgba(255, 255, 255, 0.1)',
                color: '#fff',
                cursor: 'pointer',
                padding: '6px 10px',
                borderRadius: '999px',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                transition: 'all 0.2s',
                fontSize: '12px',
                fontWeight: 500
              }}
            >
              <Mic size={14} />
              <span>Voice</span>
              {voiceState && voiceState.status === 'active' && (
                <span
                  data-testid="voice-live-dot"
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    background: '#f87171',
                    display: 'inline-block'
                  }}
                />
              )}
            </button>
            {/* Session Export menu */}
            <div ref={exportMenuRef} style={{ position: 'relative' }}>
              <button
                onClick={() => setShowExportMenu(prev => !prev)}
                title="Export Session"
                aria-label="Export Session"
                style={{
                  background: showExportMenu ? 'rgba(167, 139, 250, 0.25)' : 'rgba(255, 255, 255, 0.1)',
                  border: '1px solid rgba(255, 255, 255, 0.1)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px',
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  transition: 'all 0.2s'
                }}
              >
                <Download size={14} />
              </button>
              {showExportMenu && (
                <div style={{
                  position: 'absolute',
                  top: '100%',
                  right: 0,
                  marginTop: '6px',
                  background: 'rgba(23, 23, 30, 0.95)',
                  backdropFilter: 'blur(20px)',
                  border: '1px solid rgba(255, 255, 255, 0.12)',
                  borderRadius: '8px',
                  padding: '4px',
                  minWidth: '140px',
                  zIndex: 1100,
                  boxShadow: '0 8px 32px rgba(0, 0, 0, 0.4)'
                }}>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('markdown'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: '#fff', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    Markdown (.md)
                  </button>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('json'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: '#fff', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    JSON (.json)
                  </button>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('zip'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: '#fff', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    ZIP (.zip)
                  </button>
                </div>
              )}
            </div>

            {/* Phase 10.3 C2: isMinimized used to be settable by nothing —
                the state existed, the control did not. This toggle wires
                it: minimise collapses the feed + input to the header bar,
                restore brings them back. Minimize2/Maximize2 were already
                imported and unused for this. */}
            {isMinimized ? (
              <button 
                onClick={() => setIsMinimized(false)}
                title="Restore chat"
                aria-label="Restore chat"
                style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <Maximize2 size={14} />
              </button>
            ) : (
              <button 
                onClick={() => setIsMinimized(true)}
                title="Minimise chat"
                aria-label="Minimise chat"
                style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <Minimize2 size={14} />
              </button>
            )}
            {isFloating ? (
              <>
                <button 
                  onClick={() => window.electronAPI && window.electronAPI.closeFloatingChat()}
                  title="Re-attach to Main Window"
                  style={{ WebkitAppRegion: 'no-drag', background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <LogIn size={14} />
                </button>
                <button 
                  onClick={() => window.electronAPI && window.electronAPI.closeFloatingChat()}
                  title="Close Window"
                  style={{ WebkitAppRegion: 'no-drag', background: 'rgba(239, 68, 68, 0.2)', border: '1px solid rgba(239, 68, 68, 0.4)', color: '#fca5a5', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <X size={14} />
                </button>
              </>
            ) : (
              <button 
                onClick={() => onDetach({ input })}
                title="Pop out to floating window"
                style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <ExternalLink size={14} />
              </button>
            )}
          </div>
        </div>

        {/* Chat Feed */}
        {!isMinimized && (
          <>
            <div className="chat-scroll" style={{
              flex: 1,
              padding: '20px',
              overflowY: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px'
            }}>
              {messages.length === 0 && !isThinking && (
                <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#9090a0', textAlign: 'center' }}>
                  <p>No active session. Open a project or start a new chat.</p>
                </div>
              )}

              {messages.map((msg, i) => {
                if (msg.role === 'system') {
                  return (
                    <div key={i} style={{ 
                      display: 'flex', 
                      alignItems: 'center', 
                      gap: '12px', 
                      margin: '8px 0', 
                      padding: '12px 16px', 
                      background: 'rgba(255, 255, 255, 0.03)', 
                      borderRadius: '12px',
                      color: '#9090a0',
                      fontSize: '13px'
                    }}>
                      <AlertCircle size={16} />
                      <span style={{ flex: 1 }}>{msg.content}</span>
                    </div>
                  );
                }

                if (msg.role === 'tool_approval') {
                  return (
                    <div key={i} style={{ 
                      margin: '8px 0', 
                      background: 'rgba(0, 0, 0, 0.4)', 
                      border: '1px solid rgba(239, 68, 68, 0.3)',
                      borderRadius: '16px',
                      overflow: 'hidden',
                      boxShadow: '0 8px 32px rgba(0, 0, 0, 0.2)'
                    }}>
                      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', borderBottom: '1px solid rgba(255, 255, 255, 0.05)', background: 'rgba(239, 68, 68, 0.1)' }}>
                        <ShieldAlert size={20} color="#ef4444" />
                        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: '#fca5a5' }}>Execution Approval Required</h3>
                      </div>
                      <div style={{ padding: '20px' }}>
                        <p style={{ margin: '0 0 16px 0', fontSize: '14px', color: '#e2e8f0', lineHeight: 1.5 }}>
                          The agent has requested permission to execute <strong>{msg.tool_name}</strong>.
                        </p>
                        <div style={{ background: 'rgba(0,0,0,0.5)', padding: '12px', borderRadius: '8px', fontFamily: 'monospace', fontSize: '12px', color: '#a78bfa', marginBottom: '20px', overflowX: 'auto' }}>
                          {JSON.stringify(msg.tool_args, null, 2)}
                        </div>
                        {msg.preview && (
                          <div style={{ marginBottom: '20px' }}>
                            <ApprovalPreview preview={msg.preview} />
                          </div>
                        )}
                        {!approvalArgEdits[msg.id] ? (
                          <button
                            data-testid="approval-edit-args-btn"
                            onClick={() => setApprovalArgEdits(prev => ({ ...prev, [msg.id]: { text: JSON.stringify(msg.tool_args || {}, null, 2), error: '' } }))}
                            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', marginBottom: '20px' }}>
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
                              style={{ width: '100%', boxSizing: 'border-box', background: 'rgba(0,0,0,0.5)', color: '#a78bfa', border: '1px solid rgba(255, 255, 255, 0.15)', borderRadius: '8px', padding: '10px', fontFamily: 'monospace', fontSize: '12px' }}
                            />
                            {approvalArgEdits[msg.id].error && (
                              <div data-testid="approval-args-error" style={{ fontSize: '12px', color: '#fca5a5', marginTop: '6px' }}>{approvalArgEdits[msg.id].error}</div>
                            )}
                          </div>
                        )}
                        <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                          <button 
                            onClick={() => onApproveTool && onApproveTool(msg.id, false, msg.approval_key)}
                            className="ios-close-btn"
                            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', display: 'flex', alignItems: 'center' }}
                          >
                            <X size={16} style={{ marginRight: '6px' }} />
                            Reject Request
                          </button>
                          {approvalScopePath(msg.tool_args) && (
                            <>
                              <button 
                                onClick={() => approveToolWithEdits(msg, 'file')}

                                className="ios-close-btn"
                                style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', display: 'flex', alignItems: 'center' }}
                              >
                                Approve + remember file
                              </button>
                              <button 
                                onClick={() => approveToolWithEdits(msg, 'folder')}

                                className="ios-close-btn"
                                style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', display: 'flex', alignItems: 'center' }}
                              >
                                Approve + remember folder
                              </button>
                            </>
                          )}
                          <button 
                            onClick={() => approveToolWithEdits(msg)}

                            className="ios-close-btn"
                            style={{ background: 'rgba(239, 68, 68, 0.2)', border: '1px solid rgba(239, 68, 68, 0.4)', color: '#fca5a5', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
                          >
                            <TerminalSquare size={16} style={{ marginRight: '6px' }} />
                            Approve Execution
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                }

                if (msg.role === 'plugin_approval') {
                  return (
                    <div key={i} style={{ 
                      margin: '8px 0', 
                      background: 'rgba(0, 0, 0, 0.4)', 
                      border: '1px solid rgba(59, 130, 246, 0.3)',
                      borderRadius: '16px',
                      overflow: 'hidden',
                      boxShadow: '0 8px 32px rgba(0, 0, 0, 0.2)'
                    }}>
                      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: '12px', borderBottom: '1px solid rgba(255, 255, 255, 0.05)', background: 'rgba(59, 130, 246, 0.1)' }}>
                        <ShieldAlert size={20} color="#3b82f6" />
                        <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600, color: '#93c5fd' }}>Plugin Install Approval Required</h3>
                      </div>
                      <div style={{ padding: '20px' }}>
                        <p style={{ margin: '0 0 16px 0', fontSize: '14px', color: '#e2e8f0', lineHeight: 1.5 }}>
                          The agent has requested permission to install plugin <strong>{msg.plugin_name}</strong>.
                        </p>
                        <div style={{ background: 'rgba(0,0,0,0.5)', padding: '12px', borderRadius: '8px', fontFamily: 'monospace', fontSize: '12px', color: '#60a5fa', marginBottom: '20px', overflowX: 'auto' }}>
                          {JSON.stringify(msg.plugin_details, null, 2)}
                        </div>
                        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
                          <button 
                            onClick={() => onApprovePlugin && onApprovePlugin(msg.id, false)}
                            className="ios-close-btn"
                            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', display: 'flex', alignItems: 'center' }}
                          >
                            <X size={16} style={{ marginRight: '6px' }} />
                            Reject Request
                          </button>
                          <button 
                            onClick={() => onApprovePlugin && onApprovePlugin(msg.id, true)}
                            className="ios-close-btn"
                            style={{ background: 'rgba(59, 130, 246, 0.2)', border: '1px solid rgba(59, 130, 246, 0.4)', color: '#93c5fd', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
                          >
                            <TerminalSquare size={16} style={{ marginRight: '6px' }} />
                            Approve Install
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                }

                // Epic 8, E8-1.1a: this message's recorded rewind
                // point for the step menu (null without a manifest).
                const rewindPoint = rewindManifest
                  ? getRewindPointForMessage(messages, i, rewindManifest)
                  : null;

                return (
                  <div key={i} className={`chat-message ${msg.role === 'user' ? 'user' : 'agent'}`} style={{
                    display: 'flex',
                    flexDirection: msg.role === 'user' ? 'row-reverse' : 'row',
                    gap: '12px',
                    alignItems: 'flex-start'
                  }}>
                    <div style={{
                      width: '32px',
                      height: '32px',
                      borderRadius: '10px',
                      background: msg.role === 'user' ? 'rgba(255,255,255,0.1)' : 'rgba(var(--accent-rgb), 0.2)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0
                    }}>
                      {msg.role === 'user' ? <User size={18} color="#fff" /> : <Bot size={18} color="#a78bfa" />}
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
                            if (onEditMessage) onEditMessage(msg, i);
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
                      {rewindManifest && (
                        <button
                          type="button"
                          className="msg-step-menu-btn"
                          data-testid="step-menu-btn"
                          title="Step actions"
                          aria-label="Step actions for this message"
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            setOpenStepMenu(openStepMenu === i ? null : i);
                          }}
                        >
                          <MoreHorizontal size={12} />
                        </button>
                      )}
                      {rewindManifest && openStepMenu === i && rewindPoint && (
                        <div className="step-menu" data-testid="step-menu">
                          {rewindPoint.scope === 'current' && rewindPoint.entry && (
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
                          {rewindPoint.scope === 'current' && rewindPoint.entry ? (
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
                          )}
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
                                  step_index: rewindPoint.scope === 'current' && rewindPoint.entry
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
                          pendingApprovals={messages.filter(m => m.role === 'tool_approval')}
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
                                  <div style={{ padding: '4px 8px', background: 'rgba(255,255,255,0.1)', borderRadius: '6px', fontSize: '12px' }}>
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
                          style={{ marginTop: '6px', fontSize: '10px', letterSpacing: '0.08em', textTransform: 'uppercase', color: 'rgba(255,255,255,0.55)' }}
                        >
                          Queued
                        </div>
                      )}
                      {i === lastFinalAgentIndex && !isThinking && onRegenerate && (
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
              })}


              {/* Voice live mirror (Epic 8, E8-5.1a): provisional
                  bubbles — the partial transcript and the streaming
                  reply. Finals commit as real messages via App. */}
              {voiceState && voiceState.partialText && (
                <div className="chat-message user" data-testid="voice-partial-bubble" style={{ display: 'flex', flexDirection: 'row-reverse', gap: '12px', padding: '16px 20px', opacity: 0.75 }}>
                  <div style={{ background: 'rgba(255,255,255,0.1)', borderRadius: '50%', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                    <User size={18} color="#fff" />
                  </div>
                  <div style={{ fontSize: '14px', lineHeight: 1.6, fontStyle: 'italic' }}>{voiceState.partialText}</div>
                </div>
              )}
              {voiceState && voiceState.assistantText && (
                <div className="chat-message agent" data-testid="voice-assistant-bubble" style={{ display: 'flex', gap: '12px', padding: '16px 20px' }}>
                  <div style={{ background: 'rgba(var(--accent-rgb), 0.2)', borderRadius: '50%', width: '36px', height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                    <Bot size={18} color="#a78bfa" />
                  </div>
                  <div style={{ fontSize: '14px', lineHeight: 1.6 }}>{voiceState.assistantText}</div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Agent browser task card (Epic 8, E8-6.1) */}
            {activeBrowserSession && (
              <BrowserTaskCard
                session={activeBrowserSession}
                onControl={handleBrowserControlClick}
              />
            )}

            {/* Scratchpad panel (Epic 1, E1-17): above the composer, below the message stream */}
            <ScratchpadPanel scratchpad={scratchpad} />

            {/* Input Area */}
            <div style={{ padding: '16px', borderTop: '1px solid rgba(255,255,255,0.08)', position: 'relative' }}>
              {showSlashPopup && filteredCommands.length > 0 && (
                <div 
                  ref={slashPopupRef}
                  className="chat-scroll"
                  style={{
                    position: 'absolute',
                    bottom: '100%',
                    left: '16px',
                    right: '16px',
                    marginBottom: '8px',
                    background: 'rgba(30, 41, 59, 0.95)',
                    backdropFilter: 'blur(12px)',
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: '12px',
                    padding: '8px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '4px',
                    maxHeight: '200px',
                    overflowY: 'auto',
                    zIndex: 100,
                    boxShadow: '0 -10px 40px rgba(0,0,0,0.5)'
                  }}
                >
                  {filteredCommands.map((cmd, idx) => (
                    <div
                      key={cmd.command}
                      onClick={() => {
                        setInput(cmd.command + ' ');
                        setShowSlashPopup(false);
                        textareaRef.current?.focus();
                      }}
                      style={{
                        padding: '8px 12px',
                        borderRadius: '8px',
                        background: idx === slashIndex ? 'rgba(59, 130, 246, 0.2)' : 'transparent',
                        cursor: 'pointer',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '4px'
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span style={{ color: idx === slashIndex ? '#60a5fa' : '#e2e8f0', fontWeight: 'bold' }}>{cmd.command}</span>
                        {cmd.type === 'skill_invoke' && (
                          <span style={{ fontSize: '10px', background: 'rgba(52, 211, 153, 0.2)', color: '#6ee7b7', padding: '2px 6px', borderRadius: '4px' }}>SKILL</span>
                        )}
                      </div>
                      <span style={{ color: '#94a3b8', fontSize: '12px' }}>{cmd.description}</span>
                    </div>
                  ))}
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: '8px' }}>
                <button
                  type="button"
                  className={`plan-mode-toggle ${planMode ? 'active' : ''}`}
                  aria-pressed={planMode}
                  aria-label="Toggle plan mode"
                  title="Plan mode: agents only use read-only tools"
                  onClick={() => onTogglePlanMode && onTogglePlanMode()}
                >
                  Plan
                </button>
                <button
                  type="button"
                  className="snip-btn"
                  data-testid="snip-ask-btn"
                  title="Snip & ask (Ctrl+Shift+S)"
                  aria-label="Snip screen region"
                  onClick={handleTriggerSnip}
                  style={{
                    marginLeft: '8px',
                    background: 'rgba(255,255,255,0.06)',
                    border: '1px solid rgba(255,255,255,0.12)',
                    borderRadius: '8px',
                    padding: '4px 10px',
                    color: '#cbd5e1',
                    fontSize: '12px',
                    fontWeight: 500,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    transition: 'all 0.15s',
                  }}
                >
                  <Scissors size={13} color="#a78bfa" />
                  Snip &amp; ask
                </button>
              </div>
              <form onSubmit={handleSubmit} className="ios-glass-input" style={{ 
                display: 'flex', 
                flexDirection: 'column',
                padding: '8px 12px' 
              }}>
                {attachments.length > 0 && (
                  <div data-testid="composer-attachments-preview" style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '8px', width: '100%' }}>
                    {attachments.map((att, idx) => (
                      <div key={idx} style={{ position: 'relative', display: 'inline-block' }}>
                        <img
                          data-testid="composer-attachment-thumbnail"
                          src={att.data || att.url || (att.path ? `file://${att.path}` : '')}
                          alt={att.name || 'Attached snip'}
                          style={{ width: '56px', height: '56px', objectFit: 'cover', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.2)' }}
                        />
                        <button
                          type="button"
                          data-testid="remove-attachment-btn"
                          onClick={() => handleRemoveAttachment(idx)}
                          style={{
                            position: 'absolute',
                            top: '-6px',
                            right: '-6px',
                            width: '18px',
                            height: '18px',
                            borderRadius: '50%',
                            background: '#ef4444',
                            color: '#fff',
                            border: 'none',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            cursor: 'pointer',
                            padding: 0,
                          }}
                          title="Remove attachment"
                        >
                          <X size={10} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                <div style={{ display: 'flex', width: '100%', alignItems: 'flex-end' }}>
                <textarea 
                  ref={textareaRef}
                  value={input}
                  onChange={handleInputChange}
                  onKeyDown={handleInputKeyDown}
                  placeholder="Message Aether..."
                  style={{
                    flex: 1,
                    background: 'transparent',
                    border: 'none',
                    color: '#fff',
                    outline: 'none',
                    resize: 'none',
                    minHeight: '24px',
                    maxHeight: '120px',
                    fontSize: '14px',
                    padding: '4px 0',
                    fontFamily: 'inherit'
                  }}
                  rows={1}
                />
                {isThinking ? (
                  <>
                  {input.trim() && (
                    <button
                      type="submit"
                      title="Queue steering message"
                      aria-label="Queue steering message"
                      style={{
                        background: 'var(--accent)',
                        border: 'none',
                        borderRadius: '50%',
                        width: '32px',
                        height: '32px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: '#fff',
                        cursor: 'pointer',
                        transition: 'all 0.2s',
                        marginLeft: '8px',
                        flexShrink: 0
                      }}
                    >
                      <Send size={14} />
                    </button>
                  )}
                  <button 
                    type="button"
                    onClick={onStopTask}
                    title="Stop generation"
                    aria-label="Stop generation"
                    className="ios-chat-stop-btn"
                    style={{
                      background: 'rgba(239, 68, 68, 0.2)',
                      border: '1px solid rgba(239, 68, 68, 0.4)',
                      borderRadius: '50%',
                      width: '32px',
                      height: '32px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: '#ef4444',
                      cursor: 'pointer',
                      transition: 'all 0.2s',
                      marginLeft: '8px',
                      flexShrink: 0,
                      boxShadow: '0 0 10px rgba(239, 68, 68, 0.25)'
                    }}
                  >
                    <Square size={12} fill="currentColor" />
                  </button>
                  </>
                ) : (
                  <button 
                    type="submit"
                    disabled={!input.trim() && attachments.length === 0}
                    style={{
                      background: (input.trim() || attachments.length > 0) ? 'var(--accent)' : 'rgba(255,255,255,0.1)',
                      border: 'none',
                      borderRadius: '50%',
                      width: '32px',
                      height: '32px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: (input.trim() || attachments.length > 0) ? '#fff' : 'rgba(255,255,255,0.4)',
                      cursor: (input.trim() || attachments.length > 0) ? 'pointer' : 'default',
                      transition: 'all 0.2s',
                      marginLeft: '8px',
                      flexShrink: 0
                    }}
                  >
                    <Send size={14} />
                  </button>
                )}
                </div>
              </form>
              <div style={{ fontSize: '10px', color: 'rgba(255,255,255,0.3)', textAlign: 'center', marginTop: '8px' }}>
                Press Enter to send, Shift+Enter for new line
              </div>
            </div>
          </>
        )}

        {/* Epic 8, E8-1.1b: 5-second rewind undo window. Expiry is
            reported through onRewindUndoExpire so App can tell an
            expiry apart from an Undo click. */}
        {rewindUndo && (
          <UndoToast
            expiresAt={rewindUndo.expiresAt}
            onUndo={() => { if (onRewindUndo) onRewindUndo(); }}
            onExpire={() => { if (onRewindUndoExpire) onRewindUndoExpire(); }}
          />
        )}

        {/* Copy Success Floating Toast */}
        {toastMessage && (
          <div className="ios-copy-toast">
            <Check size={14} color="#10b981" />
            <span>{toastMessage}</span>
          </div>
        )}

        {voiceState && (
          <VoiceOverlay
            open
            active={voiceState.status === 'active'}
            status={voiceState.status === 'active' ? (voiceState.assistantText ? 'speaking' : 'listening') : voiceState.status}
            partialText={voiceState.partialText || ''}
            assistantText={voiceState.assistantText || ''}
            error={voiceState.error || null}
            webrtcConfig={voiceState.webrtcConfig || null}
            audioChunk={voiceAudioChunk}
            playbackCut={voicePlaybackCut}
            onStop={onVoiceStop}
            onDismiss={onVoiceDismissError}
          />
        )}

        <SnipOverlay isOpen={isSnipOverlayOpen} onCapture={handleSnipCaptured} onCancel={() => setIsSnipOverlayOpen(false)} />

        {/* Apple iOS Glass Context Menu */}
        <ContextMenu 
          menu={contextMenu} 
          onClose={() => setContextMenu(null)} 
          onShowToast={showToast}
          setInput={setInput}
        />
      </div>
  );
}
