import React, { useState, useEffect, useRef } from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import { Send, Settings, CheckCircle2, Circle, Bot, User, Trash2, X, Plus, TerminalSquare, MessageSquare, LayoutDashboard, Folder, BarChart2, Minus, Square, LogIn } from 'lucide-react';
import DraggableChatWindow from './DraggableChatWindow';
import SettingsPanel from './SettingsPanel';
import Dashboard from './Dashboard';
import Projects from './Projects';
import './chat.css';

// Dummy graph data for "first-run" state
const initGraphData = {
  nodes: [
    { id: 'AETHER-OS', group: 1, val: 20 },
    { id: 'CORE_BRAIN', group: 2, val: 10 },
    { id: 'LOG_INIT', group: 3, val: 5 },
    { id: 'SYSTEM_STATE', group: 2, val: 8 },
    { id: 'KNOWLEDGE_BASE', group: 2, val: 12 }
  ],
  links: [
    { source: 'AETHER-OS', target: 'CORE_BRAIN' },
    { source: 'AETHER-OS', target: 'LOG_INIT' },
    { source: 'CORE_BRAIN', target: 'SYSTEM_STATE' },
    { source: 'CORE_BRAIN', target: 'KNOWLEDGE_BASE' },
    { source: 'SYSTEM_STATE', target: 'LOG_INIT' }
  ]
};

// Module-level IPC event deduplicator to drop identical packets fired by multiple listeners
let lastRawIpcData = null;
let lastRawIpcTime = 0;

function App() {
  const [graphData, setGraphData] = useState(initGraphData);
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('Initializing');
  const [isThinking, setIsThinking] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [isChatDetached, setIsChatDetached] = useState(false);
  
  // Settings State
  const [providers, setProviders] = useState({});
  const [localModels, setLocalModels] = useState([]);
  const [defaultModel, setDefaultModel] = useState('');

  // Dashboard State (real data, not hardcoded)
  const [activeTaskIds, setActiveTaskIds] = useState(new Set());
  const [pluginCount, setPluginCount] = useState(0);
  const [memoryMb, setMemoryMb] = useState(null);

  const params = new URLSearchParams(window.location.search);
  const isFloating = params.get('floating') === 'true';
  const initialSession = params.get('session');

  const [sessions, setSessions] = useState([]);
  const [activeSession, setActiveSession] = useState(initialSession || null);
  const activeSessionRef = useRef(initialSession || null);
  useEffect(() => { activeSessionRef.current = activeSession; }, [activeSession]);
  const [activeTab, setActiveTab] = useState('Chat'); // 'Chat', 'Dashboard', 'Projects', 'Settings'
  const [initialInput, setInitialInput] = useState('');
  
  useEffect(() => {
    if (isFloating) {
      try {
        const transferData = localStorage.getItem('floating_transfer_state');
        if (transferData) {
          const parsed = JSON.parse(transferData);
          if (parsed.messages) setChatMessages(parsed.messages);
          if (parsed.input) setInitialInput(parsed.input);
          // We intentionally don't clear it immediately so refreshing the floating window keeps the context
        }
      } catch (err) {
        console.error('Failed to load floating transfer state', err);
      }
    }
  }, [isFloating]);

  // Sync back to floating_transfer_state if messages change in floating window
  useEffect(() => {
    if (isFloating && chatMessages.length > 0) {
      try {
        const current = JSON.parse(localStorage.getItem('floating_transfer_state') || '{}');
        current.messages = chatMessages;
        localStorage.setItem('floating_transfer_state', JSON.stringify(current));
      } catch (e) {}
    }
  }, [isFloating, chatMessages]);

  const wsRef = useRef(null);
  const graphRef = useRef();
  const pendingSessionSwitchRef = useRef(null);

  useEffect(() => {
    const unsubscribers = [];
    async function initConnection() {
      // In the new architecture, the main process handles the WebSocket.
      // We just mock wsRef so existing code can call wsRef.current.send()
      const wsListeners = new Map();

      wsRef.current = {
        readyState: 1, // Simulate WebSocket.OPEN
        send: (data) => {
          if (window.electronAPI && window.electronAPI.sendEngineMessage) {
            window.electronAPI.sendEngineMessage(data);
          }
        },
        addEventListener: (event, handler) => {
          if (event === 'message' && window.electronAPI && window.electronAPI.onEngineMessage) {
            const cleanup = window.electronAPI.onEngineMessage((data) => {
              handler({ data });
            });
            wsListeners.set(handler, cleanup);
          }
        },
        removeEventListener: (event, handler) => {
          if (event === 'message' && wsListeners.has(handler)) {
            const cleanup = wsListeners.get(handler);
            if (typeof cleanup === 'function') cleanup();
            wsListeners.delete(handler);
          }
        }
      };

      // Check initial status
      if (window.electronAPI && window.electronAPI.getWsStatus) {
        const isConnected = await window.electronAPI.getWsStatus();
        if (isConnected) {
          setStatus('Online');
          wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        }
      }

      if (window.electronAPI && window.electronAPI.onEngineMessage) {
        unsubscribers.push(window.electronAPI.onEngineMessage((data) => {
          try {
            const parsed = JSON.parse(data);
            if (parsed.type === '_ws_status') {
              if (parsed.status === 'connected') {
                setStatus('Online');
                wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
                wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
                wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
                wsRef.current.send(JSON.stringify({ type: 'PLUGIN_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
              } else {
                setStatus('Offline / Engine Disconnected');
                // If the engine crashes or drops connection, forcefully terminate any pending tasks so the UI doesn't hang spinning forever.
                setIsThinking(false);
                setChatMessages(prev => {
                  let newMessages = [...prev];
                  const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
                  if (lastAgentIdx !== -1) {
                    const last = newMessages[lastAgentIdx];
                    newMessages[lastAgentIdx] = { 
                      ...last, 
                      content: last.content ? `${last.content}\n\n**Error**: Connection lost (Engine crashed).` : "**Error**: Connection lost (Engine crashed).", 
                      isFinal: true,
                      hasError: true
                    };
                    return newMessages;
                  }
                  return prev;
                });
              }
              return;
            }
            // Route standard messages to handleMessage
            handleMessage({ data });
          } catch (e) {
            console.error(e);
          }
        }));
      }

      if (window.electronAPI && window.electronAPI.onChatDetached) {
        unsubscribers.push(window.electronAPI.onChatDetached(() => setIsChatDetached(true)));
      }
      
      if (window.electronAPI && window.electronAPI.onChatAttached) {
        unsubscribers.push(window.electronAPI.onChatAttached(() => {
          setIsChatDetached(false);
          try {
            const transferData = localStorage.getItem('floating_transfer_state');
            if (transferData) {
              const parsed = JSON.parse(transferData);
              if (parsed.messages) setChatMessages(parsed.messages);
            }
          } catch (e) {}
        }));
      }

      const handleMessage = (event) => {
        const now = Date.now();
        if (event.data && event.data === lastRawIpcData && (now - lastRawIpcTime) < 150) {
          // Drop identical duplicate packet dispatched by multiple listeners in the same tick
          return;
        }
        lastRawIpcData = event.data;
        lastRawIpcTime = now;

        const data = JSON.parse(event.data);
        if (data.type === 'hello') {
          setStatus(`Online / ${data.payload.active_model || 'Agent'}`);
        } else if (data.type === 'START_TASK' || data.type === 'TASK_STARTED') {
          // Track active task for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => new Set(prev).add(data.payload?.task_id || data.request_id));
          }
          setIsThinking(true);
          setChatMessages(prev => {
            if (prev.findLastIndex(m => m.role === 'agent' && !m.isFinal) === -1) {
              return [...prev, { role: 'agent', content: '', thoughts: [], isFinal: false }];
            }
            return prev;
          });
        } else if (data.type === 'TASK_PROGRESS') {
          let delta = data.payload.text_delta;
          if (delta !== undefined && delta !== null && typeof delta !== 'string') {
            delta = JSON.stringify(delta, null, 2);
          }
          const node = data.payload.node;
          const isThought = node && node !== 'assistant' && node !== 'agent';
          
          if (delta) {
            setChatMessages(prev => {
              const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
              if (lastAgentIdx !== -1) {
                const newMessages = [...prev];
                const last = newMessages[lastAgentIdx];
                if (isThought) {
                  const currentThoughts = last.thoughts || [];
                  const updatedThoughts = [...currentThoughts];
                  if (updatedThoughts.length > 0 && updatedThoughts[updatedThoughts.length - 1].node === node) {
                    const currentText = updatedThoughts[updatedThoughts.length - 1].text || '';
                    if (delta.length > 10 && currentText.endsWith(delta)) {
                      return prev;
                    }
                    updatedThoughts[updatedThoughts.length - 1].text += delta;
                  } else {
                    updatedThoughts.push({ node, text: delta });
                  }
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                } else {
                  newMessages[lastAgentIdx] = { ...last, content: (last.content || '') + delta };
                }
                return newMessages;
              } else {
                const veryLast = prev[prev.length - 1];
                if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) {
                  // Drop ghost progress packets that arrive after a task is completed/cancelled
                  return prev;
                }
                if (isThought) {
                  return [...prev, { role: 'agent', content: '', thoughts: [{ node, text: delta }], isFinal: false }];
                } else {
                  return [...prev, { role: 'agent', content: delta, thoughts: [], isFinal: false }];
                }
              }
            });
          }
        } else if (data.type === 'TASK_COMPLETED') {
          setIsThinking(false);
          // Remove from active tasks for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          let result = data.payload.response || data.payload.result || data.payload.output;
          
          setChatMessages(prev => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : last.content;
              
              newMessages[lastAgentIdx] = { ...last, content: finalContent, isFinal: true };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : '';
              return [...newMessages, { role: 'agent', content: finalContent, isFinal: true }];
            }
          });
        } else if (data.type === 'TASK_FAILED' || data.type === 'TASK_CANCELLED') {
          setIsThinking(false);
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          setChatMessages(prev => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            const errorText = `**${data.type === 'TASK_CANCELLED' ? 'Task Cancelled' : 'Task Failed'}**: ${data.payload.error || data.payload.message || 'Unknown error'}`;
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { 
                ...last, 
                content: last.content ? `${last.content}\n\n${errorText}` : errorText, 
                isFinal: true,
                hasError: true
              };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              return [...newMessages, { role: 'agent', content: errorText, isFinal: true, hasError: true }];
            }
          });
        } else if (data.type === 'SESSION_LIST_RESPONSE') {
          setSessions(data.payload.sessions || []);
          if (data.payload.sessions?.length > 0 && !activeSessionRef.current) {
            handleSessionSwitch(data.payload.sessions[0].id);
          } else if (isFloating && initialSession) {
            handleSessionSwitch(initialSession);
          }
        } else if (data.type === 'SESSION_GET_RESPONSE') {
          // Only replace chat messages if we explicitly requested a session switch and it matches exactly.
          if (data.request_id && data.request_id === pendingSessionSwitchRef.current) {
            pendingSessionSwitchRef.current = null; // Clear immediately to ignore any subsequent echoed broadcasts
            const sess = data.payload.session;
            setChatMessages((sess && sess.messages) ? sess.messages.map(m => ({...m, isFinal: true})) : []);
          }
        } else if (data.type === 'FALLBACK_STARTED') {
          setChatMessages(prev => [...prev, { 
            id: Date.now().toString(),
            role: 'system', 
            content: `⚠️ Primary model unreachable. Falling back to alternative provider...`,
            isFinal: true
          }]);
        } else if (data.type === 'FALLBACK_COMPLETED') {
          setChatMessages(prev => [...prev, { 
            id: Date.now().toString(),
            role: 'system', 
            content: `✅ Fallback successful. Routing request through ${data.payload.provider || 'backup provider'}.`,
            isFinal: true
          }]);
        } else if (data.type === 'TOOL_APPROVAL_REQUEST') {
          setIsThinking(false);
          setChatMessages(prev => [...prev, { 
            id: data.request_id || Date.now().toString(),
            role: 'tool_approval', 
            tool_name: data.payload.tool_name,
            tool_args: data.payload.tool_args,
            isFinal: true
          }]);
        } else if (data.type === 'PLUGIN_APPROVAL_REQUEST') {
          setIsThinking(false);
          setChatMessages(prev => [...prev, { 
            id: data.request_id || Date.now().toString(),
            role: 'plugin_approval', 
            plugin_name: data.payload.plugin_name,
            plugin_details: data.payload.plugin_details,
            isFinal: true
          }]);
        } else if (data.type === 'SESSION_CREATE_RESPONSE') {
          const newId = data.payload.session_id;
          setActiveSession(newId);
          setChatMessages([]);
          // Refresh list
          if (wsRef.current) {
            wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          }
        } else if (data.type === 'SESSION_DELETE_RESPONSE') {
          const deletedId = data.payload.session_id;
          setSessions(prev => {
            const next = prev.filter(s => s.id !== deletedId);
            if (activeSession === deletedId) {
              if (next.length > 0) {
                handleSessionSwitch(next[0].id);
              } else {
                setActiveSession(null);
                setChatMessages([]);
              }
            }
            return next;
          });
        } else if (data.type === 'PROVIDER_LIST_RESPONSE') {
          setProviders(data.payload.providers || {});
        } else if (data.type === 'PROVIDER_REMOVE_RESPONSE' || data.type === 'PROVIDER_SAVE_RESPONSE') {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          }
        } else if (data.type === 'MODEL_DEFAULT_CHANGED') {
          setProviders(prev => {
            if (!Array.isArray(prev)) return prev;
            return prev.map(p => {
              if (p.name === data.payload.provider) {
                return { ...p, is_default: true, default_model: data.payload.model };
              }
              return { ...p, is_default: false };
            });
          });
        } else if (data.type === 'LOCAL_MODEL_LIST_RESPONSE') {
          setLocalModels(data.payload.models || []);
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const plugins = data.payload.plugins;
          const pluginArr = Array.isArray(plugins) ? plugins : Object.values(plugins || {});
          setPluginCount(pluginArr.filter(p => p.enabled !== false).length);
        } else if (data.type === 'ERROR') {
          setIsThinking(false);
          setChatMessages(prev => {
            const errorText = `**Error:** ${data.payload.message || 'Unknown error'}`;
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { 
                ...last, 
                content: last.content ? `${last.content}\n\n${errorText}` : errorText, 
                isFinal: true,
                hasError: true
              };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = prev[prev.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return prev;
              return [...prev, { role: 'agent', content: errorText, isFinal: true, hasError: true }];
            }
          });
        }
      };

      };
    
    initConnection();

    // Poll Electron process memory usage every 5s for Dashboard
    const memoryPoll = setInterval(async () => {
      if (window.electronAPI?.getMemoryUsage) {
        try {
          const mb = await window.electronAPI.getMemoryUsage();
          setMemoryMb(mb);
        } catch (e) {
          // ignore
        }
      }
    }, 5000);
    // Initial fetch
    if (window.electronAPI?.getMemoryUsage) {
      window.electronAPI.getMemoryUsage().then(mb => setMemoryMb(mb)).catch(() => {});
    }

    // Add some random ambient nodes to simulate a vast but empty network
    const ambientNodes = [];
    const ambientLinks = [];
    for(let i=0; i<30; i++) {
      ambientNodes.push({ id: `amb_${i}`, group: 4, val: Math.random() * 2 + 1 });
      if (i > 0) {
        ambientLinks.push({ source: `amb_${i}`, target: `amb_${Math.floor(Math.random() * i)}` });
      }
    }
    setGraphData(prev => ({
      nodes: [...prev.nodes, ...ambientNodes],
      links: [...prev.links, ...ambientLinks]
    }));
    return () => {
      clearInterval(memoryPoll);
      unsubscribers.forEach(unsub => {
        if (typeof unsub === 'function') unsub();
      });
    };
  }, []);

  const handleSendMessage = (text) => {
    setChatMessages(prev => [
      ...prev, 
      { role: 'user', content: text, isFinal: true },
      { role: 'agent', content: '', thoughts: [], isFinal: false }
    ]);
    setIsThinking(true);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'START_TASK',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          prompt: text,
          session_id: activeSession
        }
      }));
    } else {
      setTimeout(() => {
        setIsThinking(false);
        setChatMessages(prev => {
          let newMessages = [...prev];
          const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
          if (lastAgentIdx !== -1) {
            newMessages[lastAgentIdx] = { ...newMessages[lastAgentIdx], content: "**Error**: Offline mode: Backend not connected.", isFinal: true, hasError: true };
            return newMessages;
          }
          return [...newMessages, { role: 'agent', content: "**Error**: Offline mode: Backend not connected.", isFinal: true, hasError: true }];
        });
      }, 1000);
    }
  };

  const handleSessionSwitch = (id) => {
    setActiveSession(id);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const reqId = `switch_${Date.now()}`;
      pendingSessionSwitchRef.current = reqId;
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_GET_REQUEST',
        schema_version: 1,
        request_id: reqId,
        payload: { session_id: id }
      }));
    }
  };

  const handleNewChat = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_CREATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { title: 'New Conversation' }
      }));
    }
  };

  const handleDeleteSession = (id, e) => {
    e.stopPropagation(); // prevent triggering handleSessionSwitch
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { session_id: id }
      }));
    }
  };

  const handleToolApproval = (requestId, approved) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: approved ? 'TOOL_APPROVAL_GRANTED' : 'TOOL_APPROVAL_REJECTED',
        schema_version: 1,
        request_id: requestId,
        payload: {}
      }));
      // Remove the approval card from chat
      setChatMessages(prev => prev.filter(m => m.id !== requestId));
      if (approved) setIsThinking(true);
    }
  };

  const handleChangeModel = (provider, model) => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'MODEL_SET_DEFAULT',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { provider, model }
      }));
    }
  };

  const handlePluginApproval = (requestId, approved) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: approved ? 'PLUGIN_APPROVAL_GRANTED' : 'PLUGIN_APPROVAL_REJECTED',
        schema_version: 1,
        request_id: requestId,
        payload: {}
      }));
      setChatMessages(prev => prev.filter(m => m.id !== requestId));
      if (approved) setIsThinking(true);
    }
  };

  const handleReattachChat = () => {
    if (window.electronAPI && window.electronAPI.closeFloatingChat) {
      window.electronAPI.closeFloatingChat();
    }
    setIsChatDetached(false);
    setActiveTab('Chat');
    try {
      const transferData = localStorage.getItem('floating_transfer_state');
      if (transferData) {
        const parsed = JSON.parse(transferData);
        if (parsed.messages) setChatMessages(parsed.messages);
      }
    } catch (e) {}
  };

  const handleStopTask = () => {
    if (wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'CANCEL_TASK',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
    setIsThinking(false);
  };

  return (
    <div style={{ width: '100vw', height: '100vh', display: 'flex', overflow: 'hidden', backgroundColor: isFloating ? 'transparent' : '#05050f', flexDirection: 'column' }}>
      
      {!isFloating && (
        <TitleBar 
          isChatDetached={isChatDetached}
          onReattachChat={handleReattachChat}
          isThinking={isThinking}
          onStopTask={handleStopTask}
        />
      )}
      
      {/* Glowing Orbs for Glassmorphism pop */}
      {!isFloating && (
        <>
          <div style={{ position: 'absolute', top: '10%', left: '20%', width: '500px', height: '500px', background: 'radial-gradient(circle, rgba(124, 58, 237, 0.25) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(80px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', animation: 'float1 20s infinite ease-in-out' }}></div>
          <div style={{ position: 'absolute', bottom: '10%', right: '15%', width: '600px', height: '600px', background: 'radial-gradient(circle, rgba(236, 72, 153, 0.2) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(100px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', animation: 'float2 25s infinite ease-in-out reverse' }}></div>
          <div style={{ position: 'absolute', top: '40%', left: '50%', width: '400px', height: '400px', background: 'radial-gradient(circle, rgba(59, 130, 246, 0.2) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(90px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', transform: 'translate(-50%, -50%)', animation: 'float3 15s infinite ease-in-out' }}></div>
        </>
      )}

      <div style={{ flex: 1, position: 'relative', display: 'flex', zIndex: 10 }}>
        {/* Sidebar Command Palette (Glassmorphism) */}
        {!isFloating && (
          <div className="glass-panel" style={{
            width: '320px',
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            position: 'absolute',
            left: 0,
            top: 0,
            padding: '24px 0',
            zIndex: 20
          }}>
            <div style={{ padding: '0 24px', marginBottom: '32px', display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div style={{ width: '32px', height: '32px', background: 'linear-gradient(135deg, #7c3aed, #ec4899)', borderRadius: '8px' }}></div>
              <div>
                <h2 style={{ fontSize: '18px', margin: 0, color: '#fff' }}>AETHER-OS</h2>
                <div style={{ fontSize: '12px', color: '#a78bfa', textTransform: 'uppercase', letterSpacing: '1px' }}>{status}</div>
              </div>
            </div>

            <nav style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '0 12px' }}>
              <NavItem icon={<MessageSquare size={18} />} label="Chat" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<LayoutDashboard size={18} />} label="Dashboard" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<Folder size={18} />} label="Projects" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<Settings size={18} />} label="Settings" activeTab={activeTab} setActiveTab={setActiveTab} />
            </nav>

            {/* Sessions Section */}
            <div style={{ flex: 1, marginTop: '32px', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <div style={{ padding: '0 24px', marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ fontSize: '12px', textTransform: 'uppercase', color: '#9090a0', letterSpacing: '1px', margin: 0 }}>Chats</h3>
                <button 
                  onClick={handleNewChat}
                  style={{ 
                  background: 'rgba(255,255,255,0.05)', 
                  border: 'none', 
                  color: '#fff',
                  borderRadius: '6px',
                  padding: '4px 8px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  cursor: 'pointer',
                  fontSize: '11px'
                }}>
                  <Plus size={12} /> New
                </button>
              </div>
              
              <div className="chat-scroll" style={{ padding: '0 12px', flex: 1, overflowY: 'auto' }}>
                {sessions.map(s => (
                  <div 
                    key={s.id} 
                    className={`session-item ${activeSession === s.id ? 'active' : ''}`}
                    onClick={() => handleSessionSwitch(s.id)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', overflow: 'hidden' }}>
                      <MessageSquare size={16} style={{ opacity: activeSession === s.id ? 1 : 0.5, flexShrink: 0 }} />
                      <span style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title || s.name || `Session ${s.id}`}</span>
                    </div>
                    <button 
                      className="session-delete-btn"
                      onClick={(e) => handleDeleteSession(s.id, e)}
                      title="Delete Session"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            </div>

          </div>
        )}

      {activeTab === 'Chat' && !isChatDetached && (
        <DraggableChatWindow 
          messages={chatMessages} 
          onSendMessage={handleSendMessage} 
          onStopTask={handleStopTask}
          onApproveTool={handleToolApproval}
          onApprovePlugin={handlePluginApproval}
          status={status}
          isThinking={isThinking}
          providers={providers}
          onChangeModel={handleChangeModel}
          onDetach={(state) => {
            localStorage.setItem('floating_transfer_state', JSON.stringify({
              messages: chatMessages,
              input: state?.input || ''
            }));
            setIsChatDetached(true);
            window.electronAPI?.openFloatingChat(activeSession);
          }}
          isFloating={isFloating}
          initialInput={initialInput}
        />
      )}

      {activeTab !== 'Chat' && (
        <div style={{
          position: 'absolute',
          top: '40px',
          left: '360px',
          right: '40px',
          bottom: '40px',
          zIndex: 40,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center'
        }}>
          <div className="ios-glass" style={{ width: '100%', height: '100%', padding: '32px', display: 'flex', flexDirection: 'column' }}>
            {activeTab === 'Settings' && (
               <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
                 <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                   <h1 style={{ color: '#fff', fontSize: '28px', margin: 0 }}>Settings</h1>
                   <button 
                     onClick={() => setActiveTab('Chat')}
                     className="ios-close-btn"
                     aria-label="Close Settings"
                   >
                     <X size={20} />
                   </button>
                 </div>
                 <SettingsPanel 
                   wsRef={wsRef} 
                   providers={providers}
                   localModels={localModels}
                   defaultModel={defaultModel}
                 />
               </div>
            )}
            {activeTab === 'Dashboard' && <Dashboard status={status} activeTasks={activeTaskIds.size} pluginCount={pluginCount} memoryMb={memoryMb} />}
            {activeTab === 'Projects' && <Projects wsRef={wsRef} />}
            {activeTab !== 'Settings' && activeTab !== 'Dashboard' && activeTab !== 'Projects' && (
               <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: '#9090a0' }}>
                 <h2>{activeTab} view coming soon.</h2>
               </div>
            )}
          </div>
        </div>
      )}

      {/* 3D Knowledge Graph Background */}
      {!isFloating && (
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 1 }}>
          <ForceGraph3D
            ref={graphRef}
            graphData={graphData}
            backgroundColor="#05050f"
            nodeColor={node => {
              if (node.group === 1) return '#ec4899'; // AETHER-OS root
              if (node.group === 2) return '#60a5fa'; // Core brain nodes
              if (node.group === 3) return '#a78bfa'; // Logs
              return '#312e81'; // Ambient
            }}
            nodeRelSize={6}
            linkOpacity={0.3}
            linkWidth={1.5}
            linkColor={() => '#4338ca'}
            enableNodeDrag={false}
            showNavInfo={false}
          />
        </div>
      )}

    </div>
    </div>
  );
}

function NavItem({ icon, label, activeTab, setActiveTab }) {
  return (
    <div 
      onClick={() => setActiveTab(label)}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        padding: '10px 14px',
        borderRadius: '10px',
        color: activeTab === label ? '#fff' : '#9090a0',
        background: activeTab === label ? 'rgba(255, 255, 255, 0.1)' : 'transparent',
        cursor: 'pointer',
        transition: 'all 0.2s',
        fontSize: '14px',
        fontWeight: activeTab === label ? '500' : '400',
      }}
      onMouseEnter={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
          e.currentTarget.style.color = '#fff';
        }
      }}
      onMouseLeave={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'transparent';
          e.currentTarget.style.color = '#9090a0';
        }
      }}
    >
      {icon}
      {label}
    </div>
  );
}

function TitleBar({ isChatDetached, onReattachChat, isThinking, onStopTask }) {
  const handleMinimize = () => window.electronAPI?.minimizeWindow();
  const handleMaximize = () => window.electronAPI?.maximizeWindow();
  const handleClose = () => window.electronAPI?.closeWindow();

  return (
    <div style={{
      height: '36px',
      background: 'transparent',
      WebkitAppRegion: 'drag', // Electron specific for dragging
      display: 'flex',
      justifyContent: 'flex-end',
      alignItems: 'center',
      zIndex: 100,
      position: 'absolute',
      top: 0,
      left: 0,
      right: 0,
      padding: '0 8px 0 16px'
    }}>
      <div style={{ WebkitAppRegion: 'no-drag', display: 'flex', alignItems: 'center', height: '100%', gap: '6px' }}>
        {/* Stop Task button shown whenever a task is active */}
        {isThinking && (
          <button 
            onClick={onStopTask}
            className="ios-stop-btn"
            title="Stop current task"
            aria-label="Stop current task"
          >
            <Square size={9} fill="currentColor" />
          </button>
        )}

        {/* Re-attach button with running task indicator */}
        {isChatDetached && (
          <button 
            onClick={onReattachChat}
            className={`ios-reattach-btn ${isThinking ? 'running' : ''}`}
            title={isThinking ? "Task is running • Click to re-attach to Main Window" : "Re-attach to Main Window"}
            aria-label="Re-attach to Main Window"
          >
            <LogIn size={13} />
            {isThinking && <span className="ios-running-badge" />}
          </button>
        )}
        <div style={{ display: 'flex', height: '100%' }}>
          <button onClick={handleMinimize} className="window-btn" title="Minimize">
            <Minus size={14} />
          </button>
          <button onClick={handleMaximize} className="window-btn" title="Maximize">
            <Square size={12} />
          </button>
          <button onClick={handleClose} className="window-btn close-btn" title="Close">
            <X size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

export default App;
