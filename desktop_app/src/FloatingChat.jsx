import React, { useState, useEffect } from 'react';
import { X, Minus, MessageSquare, Send } from 'lucide-react';
import './chat.css';

export default function FloatingChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');

  useEffect(() => {
    if (window.electronAPI && window.electronAPI.onEngineMessage) {
      window.electronAPI.onEngineMessage((data) => {
        try {
          const parsed = JSON.parse(data);
          // Simplified message handler for demo
          if (parsed.type === 'TASK_COMPLETED') {
             const result = parsed.payload.response || parsed.payload.result || parsed.payload.output;
             setMessages(prev => [...prev, { role: 'agent', content: result }]);
          } else if (parsed.type === 'TASK_PROGRESS' && parsed.payload.text_delta) {
             // Handle streaming delta if needed
          }
        } catch (e) {}
      });
    }
  }, []);

  const handleSend = () => {
    if (!input.trim()) return;
    setMessages(prev => [...prev, { role: 'user', content: input }]);
    if (window.electronAPI && window.electronAPI.sendEngineMessage) {
      window.electronAPI.sendEngineMessage(JSON.stringify({
        type: 'START_TASK',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { prompt: input, session_id: null }
      }));
    }
    setInput('');
  };

  return (
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', backgroundColor: 'rgba(5, 5, 15, 0.85)', borderRadius: '12px', overflow: 'hidden', border: '1px solid rgba(var(--accent-rgb), 0.3)' }}>
      {/* Title Bar */}
      <div style={{ height: '36px', background: 'rgba(255,255,255,0.05)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0 12px', WebkitAppRegion: 'drag' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#fff', fontSize: '12px' }}>
          <MessageSquare size={14} color="#a78bfa" />
          Aether Chat
        </div>
        <div style={{ WebkitAppRegion: 'no-drag', display: 'flex', gap: '8px' }}>
          <button onClick={() => window.electronAPI?.minimizeWindow()} style={{ background: 'none', border: 'none', color: '#9090a0', cursor: 'pointer' }}><Minus size={14} /></button>
          <button onClick={() => window.electronAPI?.closeWindow()} style={{ background: 'none', border: 'none', color: '#9090a0', cursor: 'pointer' }}><X size={14} /></button>
        </div>
      </div>
      
      {/* Messages Area */}
      <div style={{ flex: 1, padding: '16px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '12px' }} className="chat-scroll">
        {messages.map((m, i) => (
          <div key={i} style={{ alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start', background: m.role === 'user' ? 'var(--accent)' : 'rgba(255,255,255,0.1)', color: '#fff', padding: '8px 12px', borderRadius: '12px', maxWidth: '85%', fontSize: '13px' }}>
            {m.content}
          </div>
        ))}
        {messages.length === 0 && <div style={{ color: '#9090a0', textAlign: 'center', marginTop: '40px', fontSize: '13px' }}>Start typing to interact with Aether.</div>}
      </div>

      {/* Input Area */}
      <div style={{ padding: '12px', background: 'rgba(255,255,255,0.02)', borderTop: '1px solid rgba(255,255,255,0.05)' }}>
        <div style={{ display: 'flex', gap: '8px', background: 'rgba(0,0,0,0.3)', borderRadius: '20px', padding: '4px 4px 4px 16px', alignItems: 'center', border: '1px solid rgba(255,255,255,0.1)' }}>
          <input 
            type="text" 
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSend()}
            placeholder="Message Aether..."
            style={{ flex: 1, background: 'transparent', border: 'none', color: '#fff', outline: 'none', fontSize: '13px' }}
          />
          <button onClick={handleSend} style={{ background: 'var(--accent)', border: 'none', width: '28px', height: '28px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', cursor: 'pointer' }}>
            <Send size={12} />
          </button>
        </div>
      </div>
    </div>
  );
}
