import React, { useState, useEffect, useRef } from 'react';

import { Send, Maximize2, Minimize2, X, GripHorizontal, Bot, User, AlertCircle, ShieldAlert, Check, Shield, TerminalSquare, ChevronDown, ExternalLink, LogIn, Square, CheckCircle2, Copy, Scissors, Clipboard, CheckCheck } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import Dropdown from './Dropdown';
import './chat.css';

const ToolActivityCard = ({ payload }) => {
  const [expanded, setExpanded] = useState(false);
  
  if (!payload) return null;
  
  const { tool_name, tool_args, status, result, node } = payload;
  
  let argsStr = '';
  try {
    argsStr = typeof tool_args === 'string' ? tool_args : JSON.stringify(tool_args, null, 2);
  } catch(e) { argsStr = String(tool_args); }
  
  const hasResult = result !== undefined && result !== null && result !== '';
  let resultStr = '';
  if (hasResult) {
    try {
      resultStr = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
    } catch(e) { resultStr = String(result); }
  }

  return (
    <div className="tool-activity-card">
      <div className="tool-header">
        <div className="tool-name-container">
          <TerminalSquare size={16} className="tool-icon" />
          <span>{tool_name || 'unknown_tool'}</span>
          <span style={{opacity: 0.6, fontSize: '10px'}}>[{node || 'system'}]</span>
        </div>
        <div className={`tool-status tool-status-${status}`}>
          {status}
        </div>
      </div>
      
      {argsStr && argsStr !== '{}' && (
        <div className="tool-args selectable-text">
          <div style={{opacity: 0.6, marginBottom: '4px', fontSize: '11px'}}>Arguments:</div>
          {argsStr}
        </div>
      )}
      
      {hasResult && !expanded && (
        <div className="tool-result selectable-text">
          <div style={{opacity: 0.6, marginBottom: '4px', fontSize: '11px'}}>Result Preview:</div>
          {resultStr.slice(0, 150)}{resultStr.length > 150 ? '...' : ''}
          {resultStr.length > 150 && (
            <button className="tool-show-more-btn" onClick={() => setExpanded(true)} style={{marginTop: '6px', display: 'block'}}>
              Show more
            </button>
          )}
        </div>
      )}
      
      {hasResult && expanded && (
        <div className="tool-result selectable-text">
          <div style={{opacity: 0.6, marginBottom: '4px', fontSize: '11px'}}>Full Result:</div>
          {resultStr}
          <button className="tool-show-more-btn" onClick={() => setExpanded(false)} style={{marginTop: '6px', display: 'block'}}>
            Show less
          </button>
        </div>
      )}
    </div>
  );
};

export function ThinkingAccordion({ thoughts = [], isThinking, hasError, onStopTask }) {
  const [isExpanded, setIsExpanded] = useState(false);

  // If there are no thoughts and the task has finished successfully, don't render an empty box
  if ((!thoughts || thoughts.length === 0) && !isThinking && !hasError) return null;

  return (
    <div style={{
      marginBottom: '10px',
      borderRadius: '12px',
      background: 'rgba(0, 0, 0, 0.28)',
      border: isExpanded ? '1px solid rgba(167, 139, 250, 0.25)' : '1px solid rgba(255, 255, 255, 0.08)',
      overflow: 'hidden',
      transition: 'all 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
      boxShadow: isExpanded ? '0 4px 16px rgba(0, 0, 0, 0.3)' : 'none'
    }}>
      <div 
        onClick={() => setIsExpanded(!isExpanded)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '8px 12px',
          background: isExpanded ? 'rgba(255, 255, 255, 0.05)' : 'transparent',
          color: isThinking ? '#c4b5fd' : '#9090a0',
          fontSize: '12px',
          cursor: 'pointer',
          userSelect: 'none',
          transition: 'background 0.2s',
        }}
        title="Click to inspect what the agent is doing under the hood"
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
          <span>{isThinking ? 'Agent is thinking...' : hasError ? 'Task Stopped' : `Thought for ${thoughts?.length || 0} step${thoughts?.length === 1 ? '' : 's'}`}</span>
          <span style={{ fontSize: '10.5px', opacity: 0.65, fontWeight: 400, color: '#a78bfa' }}>
            {isExpanded ? '(click to hide)' : '(click to see under the hood)'}
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
        <div style={{
          padding: '10px 12px 12px',
          fontSize: '12px',
          color: '#d4d4d8',
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          lineHeight: '1.5',
          maxHeight: '260px',
          overflowY: 'auto',
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
          background: 'rgba(0, 0, 0, 0.2)'
        }} className="chat-thought-container chat-scroll">
          {(!thoughts || thoughts.length === 0) ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', padding: '4px 0', color: '#a1a1aa' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: hasError ? '#ef4444' : '#c4b5fd' }}>
                {!hasError && <span className="dot-typing" style={{ width: '5px', height: '5px', display: 'inline-block' }}></span>}
                <span>{hasError ? 'Task was stopped before supervisor node could initialize.' : 'Initializing supervisor node & analyzing prompt requirements...'}</span>
              </div>
              <div style={{ fontSize: '11px', color: '#71717a' }}>
                Evaluating available tools, memory context, and provider route...
              </div>
            </div>
          ) : (
            thoughts.map((t, idx) => {
              if (t.type === 'TOOL_ACTIVITY') {
                return <ToolActivityCard key={idx} payload={t.payload} />;
              }
              const node = (t.node || 'node').toLowerCase();
              let badgeColor = '#a78bfa';
              let badgeBg = 'rgba(167, 139, 250, 0.15)';
              if (node.includes('tool') || node === 'execute_tool') {
                badgeColor = '#fbbf24';
                badgeBg = 'rgba(251, 191, 36, 0.15)';
              } else if (node.includes('code') || node.includes('coder')) {
                badgeColor = '#34d399';
                badgeBg = 'rgba(52, 211, 153, 0.15)';
              } else if (node.includes('research')) {
                badgeColor = '#38bdf8';
                badgeBg = 'rgba(56, 189, 248, 0.15)';
              }

              return (
                <div key={idx} style={{ marginBottom: '8px', paddingBottom: '8px', borderBottom: '1px solid rgba(255, 255, 255, 0.05)' }}>
                  <span style={{ 
                    color: badgeColor, 
                    background: badgeBg,
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontSize: '10px',
                    fontWeight: 600,
                    letterSpacing: '0.5px'
                  }}>
                    {t.node.toUpperCase()}
                  </span>
                  <div className="thought-step-content selectable-text" style={{ marginTop: '6px', whiteSpace: 'pre-wrap', opacity: 0.9, fontSize: '11.5px', userSelect: 'text', cursor: 'text' }}>
                    {t.text.trim()}
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
                      {msg.role === 'agent' && (
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
                        msg.role === 'agent' && !msg.isFinal && (
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
