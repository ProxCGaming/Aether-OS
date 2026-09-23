import React, { useState, useEffect, useRef } from 'react';

import { Send, Maximize2, Minimize2, X, GripHorizontal, Bot, User, AlertCircle, ShieldAlert, Check, Shield, TerminalSquare, ChevronDown, ExternalLink, LogIn, Square, CheckCircle2, Copy, Scissors, Clipboard, CheckCheck, ArrowRight, ArrowDownRight, ArrowUpRight, Cpu, Layers, Activity } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import Dropdown from './Dropdown';
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

export function ToolActivityCard({ payload, isNested = false }) {
  if (!payload) return null;

  const { tool_name, tool_args, status, result, node, from_node, to_node } = payload;
  const isPending = status === 'pending';
  const isFailed = status === 'failed';

  const callerNode = from_node || node || 'agent';
  const targetNode = to_node || 'execute_tool';

  return (
    <div className={`tool-activity-card ${isNested ? 'nested' : ''} ${isPending ? 'pending' : ''} ${isFailed ? 'failed' : ''}`}>
      <div className="tool-card-topbar">
        <div className="tool-identity">
          <TerminalSquare size={14} className="tool-terminal-icon" />
          <span className="tool-call-name">{tool_name || 'unknown_tool'}</span>
          <span className="tool-route-badge">
            {callerNode.toUpperCase()} ➔ {targetNode.toUpperCase()}
          </span>
        </div>
        <div className={`tool-status-pill tool-status-${status || 'pending'}`}>
          {isPending && <span className="tool-spinner-dot" />}
          {isFailed && <AlertCircle size={11} />}
          {!isPending && !isFailed && <CheckCircle2 size={11} />}
          <span>{status || 'pending'}</span>
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
        
        {isPending ? (
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

export function NodeActivityCard({ activity, nestedTools = [], rawDelta }) {
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
                <ToolActivityCard key={tIdx} payload={t.payload} isNested={true} />
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

export function ThinkingAccordion({ thoughts = [], isThinking, hasError, onStopTask }) {
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
                  />
                );
              }
              if (item.type === 'tool') {
                return (
                  <ToolActivityCard 
                    key={item.key} 
                    payload={item.payload} 
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
  onDetach,
  isFloating,
  initialInput = ''
}) {
  const [input, setInput] = useState(initialInput);
  const [isMinimized, setIsMinimized] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);
  const [toastMessage, setToastMessage] = useState('');
  const toastTimeoutRef = useRef(null);
  const messagesEndRef = useRef(null);
  const nodeRef = useRef(null);
  const textareaRef = useRef(null);

  const showToast = (msg) => {
    setToastMessage(msg);
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    toastTimeoutRef.current = setTimeout(() => {
      setToastMessage('');
    }, 2000);
  };

  useEffect(() => {
    const handleGlobalClick = () => setContextMenu(null);
    const handleGlobalKeyDown = (e) => {
      if (e.key === 'Escape') setContextMenu(null);
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
    if (input.trim() && !isThinking) {
      onSendMessage(input.trim());
      setInput('');
    }
  };

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
        left: '360px',
        right: '40px',
        bottom: '40px',
        width: 'auto',
        height: 'auto',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
        transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)'
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
          <div style={{ display: 'flex', gap: '12px' }}>
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
                        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
                          <button 
                            onClick={() => onApproveTool && onApproveTool(msg.id, false)}
                            className="ios-close-btn"
                            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', padding: '8px 16px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', color: '#fff', display: 'flex', alignItems: 'center' }}
                          >
                            <X size={16} style={{ marginRight: '6px' }} />
                            Reject Request
                          </button>
                          <button 
                            onClick={() => onApproveTool && onApproveTool(msg.id, true)}
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
                    <div className={`chat-bubble ${msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-agent'}`}>
                      {(msg.role === 'agent' || msg.role === 'assistant') && (
                        <ThinkingAccordion thoughts={msg.thoughts} isThinking={!msg.isFinal} hasError={msg.hasError} onStopTask={onStopTask} />
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
                    </div>
                  </div>
                );
              })}


              <div ref={messagesEndRef} />
            </div>

            {/* Input Area */}
            <div style={{ padding: '16px', borderTop: '1px solid rgba(255,255,255,0.08)' }}>
              <form onSubmit={handleSubmit} className="ios-glass-input" style={{ 
                display: 'flex', 
                alignItems: 'flex-end',
                padding: '8px 12px' 
              }}>
                <textarea 
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSubmit(e);
                    }
                  }}
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
                ) : (
                  <button 
                    type="submit"
                    disabled={!input.trim()}
                    style={{
                      background: input.trim() ? 'var(--accent)' : 'rgba(255,255,255,0.1)',
                      border: 'none',
                      borderRadius: '50%',
                      width: '32px',
                      height: '32px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: input.trim() ? '#fff' : 'rgba(255,255,255,0.4)',
                      cursor: input.trim() ? 'pointer' : 'default',
                      transition: 'all 0.2s',
                      marginLeft: '8px',
                      flexShrink: 0
                    }}
                  >
                    <Send size={14} />
                  </button>
                )}
              </form>
              <div style={{ fontSize: '10px', color: 'rgba(255,255,255,0.3)', textAlign: 'center', marginTop: '8px' }}>
                Press Enter to send, Shift+Enter for new line
              </div>
            </div>
          </>
        )}

        {/* Copy Success Floating Toast */}
        {toastMessage && (
          <div className="ios-copy-toast">
            <Check size={14} color="#10b981" />
            <span>{toastMessage}</span>
          </div>
        )}

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
