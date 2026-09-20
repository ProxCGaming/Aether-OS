import React, { useState, useEffect, useRef } from 'react';
import Draggable from 'react-draggable';
import { Send, Maximize2, Minimize2, X, GripHorizontal, Bot, User, AlertCircle, ShieldAlert, Check, Shield, TerminalSquare, ChevronDown, ExternalLink, LogIn } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import Dropdown from './Dropdown';
import './chat.css';

export function ThinkingAccordion({ thoughts, isThinking }) {
  const [isExpanded, setIsExpanded] = useState(false);

  if (!thoughts || thoughts.length === 0) return null;

  return (
    <div style={{
      marginBottom: '12px',
      borderRadius: '8px',
      background: 'rgba(0, 0, 0, 0.2)',
      border: '1px solid rgba(255, 255, 255, 0.05)',
      overflow: 'hidden',
      transition: 'all 0.3s ease'
    }}>
      <button 
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '8px 12px',
          background: 'transparent',
          border: 'none',
          color: '#9090a0',
          fontSize: '12px',
          cursor: 'pointer',
          outline: 'none',
        }}
      >
        <TerminalSquare size={14} />
        <span style={{ flex: 1, textAlign: 'left', fontWeight: '500' }}>
          {isThinking ? 'Agent is thinking...' : `Analyzed ${thoughts.length} step${thoughts.length === 1 ? '' : 's'}`}
        </span>
        <ChevronDown 
          size={14} 
          style={{ 
            transition: 'transform 0.3s ease',
            transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)' 
          }} 
        />
      </button>
      
      {isExpanded && (
        <div style={{
          padding: '0 12px 12px',
          fontSize: '12px',
          color: '#a1a1aa',
          fontFamily: 'monospace',
          lineHeight: '1.5',
          maxHeight: '300px',
          overflowY: 'auto'
        }}>
          {thoughts.map((t, idx) => (
            <div key={idx} style={{ marginBottom: '8px', paddingBottom: '8px', borderBottom: '1px solid rgba(255, 255, 255, 0.05)' }}>
              <span style={{ color: '#a78bfa', fontWeight: 'bold' }}>[{t.node.toUpperCase()}]</span>
              <div style={{ marginTop: '4px', whiteSpace: 'pre-wrap', opacity: 0.9 }}>
                {t.text.trim()}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DraggableChatWindow({ 
  messages, 
  onSendMessage, 
  status, 
  isThinking,
  onApproveTool,
  onApprovePlugin,
  providers = [],
  onChangeModel = () => {},
  onDetach,
  isFloating
}) {
  const [input, setInput] = useState('');
  const [isMinimized, setIsMinimized] = useState(false);
  const messagesEndRef = useRef(null);
  const nodeRef = useRef(null);

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

  return (
    <Draggable nodeRef={nodeRef} handle=".draggable-handle" bounds="parent">
      <div 
        ref={nodeRef}
        className="ios-glass"
        style={{
          position: isFloating ? 'relative' : 'absolute',
          top: isFloating ? '0' : '20px',
          right: isFloating ? '0' : '20px',
          width: isFloating ? '100%' : '400px',
          height: isFloating ? '100%' : (isMinimized ? 'auto' : '600px'),
          display: 'flex',
          flexDirection: 'column',
          zIndex: 50,
          overflow: 'hidden'
        }}
      >
        {/* Header - Draggable Handle */}
        <div 
          className={isFloating ? "title-bar" : "draggable-handle"}
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
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1, WebkitAppRegion: 'no-drag' }}>
            <GripHorizontal size={16} color="rgba(255,255,255,0.4)" />
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
                  style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <LogIn size={14} />
                </button>
                <button 
                  onClick={() => window.electronAPI && window.electronAPI.closeFloatingChat()}
                  title="Close Window"
                  style={{ background: 'rgba(239, 68, 68, 0.2)', border: '1px solid rgba(239, 68, 68, 0.4)', color: '#fca5a5', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <X size={14} />
                </button>
              </>
            ) : (
              <>
                <button 
                  onClick={onDetach}
                  title="Pop out to floating window"
                  style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
                >
                  <ExternalLink size={14} />
                </button>
                <button 
                  onClick={() => setIsMinimized(!isMinimized)}
                  title={isMinimized ? "Maximize" : "Minimize"}
                  style={{ background: 'rgba(255, 255, 255, 0.1)', border: '1px solid rgba(255, 255, 255, 0.1)', color: '#fff', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
                >
                  {isMinimized ? <Maximize2 size={14} /> : <Minimize2 size={14} />}
                </button>
              </>
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
                      background: msg.role === 'user' ? 'rgba(255,255,255,0.1)' : 'rgba(124, 58, 237, 0.2)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0
                    }}>
                      {msg.role === 'user' ? <User size={18} color="#fff" /> : <Bot size={18} color="#a78bfa" />}
                    </div>
                    <div className={`chat-bubble ${msg.role === 'user' ? 'chat-bubble-user' : 'chat-bubble-agent'}`}>
                      {msg.role === 'agent' && (
                        <ThinkingAccordion thoughts={msg.thoughts} isThinking={!msg.isFinal} />
                      )}
                      
                      {msg.content ? (
                        <ReactMarkdown>{msg.content}</ReactMarkdown>
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

              {isThinking && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', alignSelf: 'flex-start', color: '#a78bfa' }}>
                  <div className="chat-bubble-agent" style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <div className="spinner" style={{ 
                      width: '14px', height: '14px', 
                      border: '2px solid rgba(167, 139, 250, 0.3)',
                      borderTopColor: '#a78bfa', 
                      borderRadius: '50%',
                      animation: 'spin 1s linear infinite'
                    }}></div>
                    <span style={{ fontSize: '13px' }}>Agent is thinking...</span>
                  </div>
                </div>
              )}
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
                <button 
                  type="submit"
                  disabled={!input.trim() || isThinking}
                  style={{
                    background: input.trim() && !isThinking ? '#7c3aed' : 'rgba(255,255,255,0.1)',
                    border: 'none',
                    borderRadius: '50%',
                    width: '32px',
                    height: '32px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: input.trim() && !isThinking ? '#fff' : 'rgba(255,255,255,0.4)',
                    cursor: input.trim() && !isThinking ? 'pointer' : 'default',
                    transition: 'all 0.2s',
                    marginLeft: '8px',
                    flexShrink: 0
                  }}
                >
                  <Send size={14} />
                </button>
              </form>
              <div style={{ fontSize: '10px', color: 'rgba(255,255,255,0.3)', textAlign: 'center', marginTop: '8px' }}>
                Press Enter to send, Shift+Enter for new line
              </div>
            </div>
          </>
        )}
      </div>
    </Draggable>
  );
}
