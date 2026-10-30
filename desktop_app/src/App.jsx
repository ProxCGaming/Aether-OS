import React, { useState, useEffect, useRef } from 'react';
import { SocketProvider, useAetherSocket } from './contexts/SocketContext';
import ForceGraph3D from 'react-force-graph-3d';
import { Send, Settings, CheckCircle2, Circle, Bot, User, Trash2, X, Plus, TerminalSquare, MessageSquare, LayoutDashboard, Folder, BarChart2, Minus, Square, LogIn, Menu, Pencil } from 'lucide-react';
import DraggableChatWindow from './DraggableChatWindow';
import SettingsPanel from './SettingsPanel';
import Dashboard from './Dashboard';
import Projects from './Projects';
import './chat.css';

// Decorative background graph (branding, not live data — see Phase 9 B3)
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

// Epic 1, E1-16: THINKING deltas accumulate into a message's thoughts
// array — consecutive deltas from the same node merge into one entry,
// preserving arrival order. Reasoning lives ONLY in thoughts; it is
// never written into a message's content.
function appendThoughtDelta(thoughts, node, delta) {
  const next = [...(thoughts || [])];
  if (next.length > 0 && next[next.length - 1].node === node && typeof next[next.length - 1].text === 'string') {
    next[next.length - 1] = { ...next[next.length - 1], text: next[next.length - 1].text + delta };
  } else {
    next.push({ node, text: delta });
  }
  return next;
}

// Append buffered thought entries onto existing thoughts, merging the
// seam entry when both sides end/start with the same node.
function mergeThoughtEntries(base, extra) {
  let merged = [...(base || [])];
  for (const entry of extra || []) {
    if (entry && typeof entry.text === 'string') {
      merged = appendThoughtDelta(merged, entry.node, entry.text);
    }
  }
  return merged;
}

function AppContent() {
  // Phase 10.3 C1: the engine-message facade lives in SocketContext, not
  // here. AppContent consumes its wsRef/isConnected; it builds no mock of
  // its own and subscribes to nothing directly.
  const { wsRef, isConnected } = useAetherSocket();

  const [status, setStatus] = useState('Initializing');
  const [isThinking, setIsThinking] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [isChatDetached, setIsChatDetached] = useState(false);
  
  // Settings State
  // Phase 10.3 C1: providers is an ARRAY everywhere (the engine sends a
  // list; see PROVIDER_LIST_RESPONSE below). Children (DraggableChatWindow,
  // SettingsPanel, ProviderSettings) all expect [], and MODEL_DEFAULT_CHANGED
  // maps over it — it silently no-op'd while this was the initial {}.
  const [providers, setProviders] = useState([]);
  const [localModels, setLocalModels] = useState([]);

  // Dashboard State (real data, not hardcoded)
  const [activeTaskIds, setActiveTaskIds] = useState(new Set());
  const [pluginCount, setPluginCount] = useState(0);
  const [memoryMb, setMemoryMb] = useState(null);

  const params = new URLSearchParams(window.location.search);
  const isFloating = params.get('floating') === 'true';
  const initialSession = params.get('session');

  const [sessions, setSessions] = useState([]);
  // Epic 1 UI batch: per-session plan mode (E1-09 UI). Keyed by session
  // id; the '__pending' key holds the choice made before the first
  // session exists and is adopted by the session when it is created.
  const [planModeBySession, setPlanModeBySession] = useState({});
  // Epic 1, E1-17: latest scratchpad per session ({kind, content,
  // agent}), keyed like planModeBySession ('__pending' before the
  // first session exists, adopted on SESSION_CREATE_RESPONSE).
  const [scratchpadBySession, setScratchpadBySession] = useState({});
  // Epic 1, E1-16: THINKING deltas that arrive before a streaming
  // agent bubble exists are buffered per session key here and flushed
  // into the bubble's thoughts when it appears (TASK_STARTED /
  // TASK_COMPLETED), so reasoning is never dropped.
  const pendingThinkingRef = useRef({});
  // Epic 1 UI batch (E1-08): session search + inline rename state.
  const [sessionSearchQuery, setSessionSearchQuery] = useState('');
  const [sessionSearchResults, setSessionSearchResults] = useState(null);
  const sessionSearchTimerRef = useRef(null);
  const sessionSearchQueryRef = useRef('');
  const [editingSessionId, setEditingSessionId] = useState(null);
  const [editingSessionTitle, setEditingSessionTitle] = useState('');
  const [activeSession, setActiveSession] = useState(initialSession || null);
  const activeSessionRef = useRef(initialSession || null);
  useEffect(() => { activeSessionRef.current = activeSession; }, [activeSession]);
  const [activeTab, setActiveTab] = useState('Chat'); // 'Chat', 'Dashboard', 'Projects', 'Settings'
  const [initialInput, setInitialInput] = useState('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [slashCommands, setSlashCommands] = useState([]);
  
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

  const graphRef = useRef();
  const pendingSessionSwitchRef = useRef(null);
  const sessionCacheRef = useRef({});
  const activeTaskSessionRef = useRef(null);

  // Phase 10.3 C1: subscribe to engine messages through the single
  // SocketContext facade (wsRef). No mock wsRef, no direct onEngineMessage
  // subscription, no local dedup — all of that lives in the facade now.
  // Connection state (isConnected) also comes from the facade; see the
  // transition effect below.
  useEffect(() => {
    const handleMessage = (event) => {
      const data = JSON.parse(event.data);
        if (data.type === 'hello') {
          setStatus(`Online / ${data.payload.active_model || 'Agent'}`);
        } else if (data.type === 'START_TASK' || data.type === 'TASK_STARTED') {
          // Track active task for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => new Set(prev).add(data.payload?.task_id || data.request_id));
          }
          activeTaskSessionRef.current = data.payload?.session_id || activeSessionRef.current;
          setIsThinking(true);
          setChatMessages(prev => {
            // E1-16: flush any THINKING deltas buffered before this
            // bubble existed into its thoughts. Deltas that arrived
            // before the task (and its session id) was known sit
            // under '__pending', so fall back to that key.
            const thinkKey = data.payload?.session_id || activeSessionRef.current || '__pending';
            let buffered = pendingThinkingRef.current[thinkKey] || [];
            if (!buffered.length && thinkKey !== '__pending') {
              buffered = pendingThinkingRef.current.__pending || [];
              if (buffered.length) delete pendingThinkingRef.current.__pending;
            }
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx === -1) {
              if (buffered.length) delete pendingThinkingRef.current[thinkKey];
              return [...prev, { role: 'agent', content: '', thoughts: buffered, isFinal: false }];
            }
            if (buffered.length) {
              delete pendingThinkingRef.current[thinkKey];
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { ...last, thoughts: mergeThoughtEntries(last.thoughts || [], buffered) };
              return newMessages;
            }
            return prev;
          });
        } else if (data.type === 'STEERING_APPLIED') {
          // Epic 1, E1-07: the engine applied a queued steer to the
          // running task — clear the bubble's queued flag.
          const appliedId = data.payload?.message_id || data.request_id;
          if (appliedId) {
            const clearQueued = (list) => (list || []).map(m =>
              m.id === appliedId ? { ...m, queued: false, steering: false } : m);
            setChatMessages(prev => clearQueued(prev));
            Object.keys(sessionCacheRef.current).forEach((sid) => {
              sessionCacheRef.current[sid] = clearQueued(sessionCacheRef.current[sid]);
            });
          }
        } else if (data.type === 'THINKING') {
          // Epic 1, E1-16: model reasoning streams into the streaming
          // bubble's thoughts ONLY — never into content, never into
          // the persisted answer body. If no bubble is streaming yet,
          // the delta is buffered per session and flushed when the
          // bubble appears (TASK_STARTED) or finalises (TASK_COMPLETED).
          const thinkDelta = data.payload?.text_delta;
          if (typeof thinkDelta === 'string' && thinkDelta.length > 0) {
            const thinkNode = data.payload?.node || 'thinking';
            const targetSession = activeTaskSessionRef.current || activeSessionRef.current;
            const bufferKey = targetSession || '__pending';
            const applyThinking = (list) => {
              const idx = list.findLastIndex(m => m.role === 'agent' && !m.isFinal);
              if (idx === -1) return null;
              const newList = [...list];
              const last = newList[idx];
              newList[idx] = { ...last, thoughts: appendThoughtDelta(last.thoughts || [], thinkNode, thinkDelta) };
              return newList;
            };
            if (targetSession && targetSession !== activeSessionRef.current) {
              const cached = sessionCacheRef.current[targetSession] || [];
              const updated = applyThinking(cached);
              if (updated) {
                sessionCacheRef.current[targetSession] = updated;
              } else {
                pendingThinkingRef.current[bufferKey] = appendThoughtDelta(pendingThinkingRef.current[bufferKey] || [], thinkNode, thinkDelta);
              }
            } else {
              setChatMessages(prev => {
                const updated = applyThinking(prev);
                if (updated) return updated;
                pendingThinkingRef.current[bufferKey] = appendThoughtDelta(pendingThinkingRef.current[bufferKey] || [], thinkNode, thinkDelta);
                return prev;
              });
            }
          }
        } else if (data.type === 'SCRATCHPAD') {
          // Epic 1, E1-17: display-only scratchpad (diagram/sketch/
          // notes) from the engine. Keep only the latest per session;
          // the panel in DraggableChatWindow renders it.
          const padContent = data.payload?.content;
          if (typeof padContent === 'string' && padContent.length > 0) {
            const padSession = data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current || '__pending';
            const padEntry = {
              kind: data.payload?.kind || 'text',
              content: padContent,
              agent: data.payload?.agent || null,
            };
            setScratchpadBySession(prev => ({ ...prev, [padSession]: padEntry }));
          }
        } else if (data.type === 'TASK_PROGRESS') {
          let delta = data.payload.text_delta;
          if (delta !== undefined && delta !== null && typeof delta !== 'string') {
            delta = JSON.stringify(delta, null, 2);
          }
          const node = data.payload.node;
          const isThought = node && node !== 'assistant' && node !== 'agent';
          
          // Filter out supervisor delegation strings that duplicate NODE_ACTIVITY
          if (isThought && delta && typeof delta === 'string' && delta.trim().startsWith('[Supervisor] -> Delegating')) {
            return;
          }
          
          if (delta) {
            const updateMessages = (prev) => {
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
            };
            
            if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
              const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
              sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
            } else {
              setChatMessages(updateMessages);
            }
          }
        } else if (data.type === 'NODE_ACTIVITY') {
          // Tool calls are exclusively rendered and managed via TOOL_ACTIVITY
          if (data.payload?.action === 'tool_request' || data.payload?.action === 'tool_result') {
            return;
          }
          const updateMessages = (prev) => {
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              const currentThoughts = last.thoughts || [];
              const updatedThoughts = [...currentThoughts];
              
              if (data.payload.status !== 'pending') {
                const pendingIdx = updatedThoughts.findLastIndex(t => 
                  t.type === 'NODE_ACTIVITY' && 
                  t.payload?.from_node === data.payload.from_node && 
                  t.payload?.to_node === data.payload.to_node && 
                  t.payload?.status === 'pending'
                );
                if (pendingIdx !== -1) {
                  updatedThoughts[pendingIdx] = { 
                    ...updatedThoughts[pendingIdx], 
                    payload: { ...updatedThoughts[pendingIdx].payload, ...data.payload }
                  };
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                  return newMessages;
                }
              }
              
              updatedThoughts.push({
                type: 'NODE_ACTIVITY',
                payload: data.payload,
                node: data.payload.from_node || data.payload.node
              });
              
              newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
              return newMessages;
            }
            return prev;
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
          } else {
            setChatMessages(updateMessages);
          }
        } else if (data.type === 'TOOL_ACTIVITY') {
          const updateMessages = (prev) => {
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              const currentThoughts = last.thoughts || [];
              const updatedThoughts = [...currentThoughts];
              
              if (data.payload.status !== 'pending') {
                const pendingIdx = updatedThoughts.findLastIndex(t => 
                  t.type === 'TOOL_ACTIVITY' && 
                  t.payload?.tool_name === data.payload.tool_name && 
                  t.payload?.status === 'pending'
                );
                if (pendingIdx !== -1) {
                  const prevArgs = updatedThoughts[pendingIdx].payload?.tool_args;
                  const newArgs = data.payload.tool_args;
                  const finalArgs = (newArgs && Object.keys(newArgs).length > 0) ? newArgs : (prevArgs || {});
                  
                  updatedThoughts[pendingIdx] = { 
                    ...updatedThoughts[pendingIdx], 
                    payload: {
                      ...updatedThoughts[pendingIdx].payload,
                      ...data.payload,
                      tool_args: finalArgs
                    }
                  };
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                  return newMessages;
                }
              }
              
              updatedThoughts.push({
                type: 'TOOL_ACTIVITY',
                payload: data.payload,
                node: data.payload.node
              });
              
              newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
              return newMessages;
            }
            return prev;
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
          } else {
            setChatMessages(updateMessages);
          }
        } else if (data.type === 'TASK_COMPLETED') {
          setIsThinking(false);
          // Remove from active tasks for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          if (data.payload?.session_id && !activeSessionRef.current) {
            setActiveSession(data.payload.session_id);
            if (wsRef.current) {
              wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
            }
          }
          let result = data.payload.response || data.payload.result || data.payload.output;

          // E1-16: THINKING deltas still buffered (no streaming bubble
          // ever appeared) join the final message's thoughts here —
          // reasoning is preserved, and still kept out of content.
          const completionKey = activeTaskSessionRef.current || data.payload?.session_id || activeSessionRef.current || '__pending';
          const bufferedThoughts = pendingThinkingRef.current[completionKey] || [];
          if (bufferedThoughts.length) delete pendingThinkingRef.current[completionKey];

          const updateCompletedMessages = (prev) => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : last.content;
              const baseThoughts = (last.thoughts && last.thoughts.length > 0) ? last.thoughts : (data.payload.thoughts || []);
              const finalThoughts = bufferedThoughts.length ? mergeThoughtEntries(baseThoughts, bufferedThoughts) : baseThoughts;
              
              newMessages[lastAgentIdx] = { ...last, content: finalContent, thoughts: finalThoughts, isFinal: true };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : '';
              const createdThoughts = bufferedThoughts.length ? mergeThoughtEntries(data.payload.thoughts || [], bufferedThoughts) : (data.payload.thoughts || []);
              return [...newMessages, { role: 'agent', content: finalContent, thoughts: createdThoughts, isFinal: true }];
            }
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateCompletedMessages(cached);
          } else {
            setChatMessages(updateCompletedMessages);
          }
        } else if (data.type === 'TASK_FAILED' || data.type === 'TASK_CANCELLED') {
          setIsThinking(false);
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          
          const updateFailedMessages = (prev) => {
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
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateFailedMessages(cached);
          } else {
            setChatMessages(updateFailedMessages);
          }
        } else if (data.type === 'SESSION_RENAME_RESPONSE') {
          // Epic 1 UI batch (E1-08): the engine confirms renames; only
          // a confirmed rename changes the local list.
          if (data.payload?.renamed && data.payload.session_id) {
            setSessions(prev => prev.map(s => s.id === data.payload.session_id ? { ...s, title: data.payload.title } : s));
          }
        } else if (data.type === 'SESSION_TITLE_UPDATED') {
          // Epic 1 UI batch (E1-08): the engine wrote a model title for
          // a session after a task completed — reflect it in the list.
          if (data.payload?.session_id && data.payload.title) {
            setSessions(prev => prev.map(s => s.id === data.payload.session_id ? { ...s, title: data.payload.title } : s));
          }
        } else if (data.type === 'SESSION_SEARCH_RESPONSE') {
          // Epic 1 UI batch (E1-08): ignore stale answers for a query
          // the user has already replaced or cleared.
          if (data.payload && data.payload.query === sessionSearchQueryRef.current) {
            setSessionSearchResults(Array.isArray(data.payload.results) ? data.payload.results : []);
          }
        } else if (data.type === 'SESSION_LIST_RESPONSE') {
          setSessions(data.payload.sessions || []);
          if (data.payload.sessions?.length > 0 && !activeSessionRef.current) {
            handleSessionSwitch(data.payload.sessions[0].id);
          } else if (isFloating && initialSession) {
            handleSessionSwitch(initialSession);
          }
        } else if (data.type === 'SLASH_COMMAND_LIST_RESPONSE') {
          setSlashCommands(data.payload.commands || []);
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE' || data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'SLASH_COMMAND_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            wsRef.current.send(JSON.stringify({ type: 'SKILL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
          }
        } else if (data.type === 'SESSION_GET_RESPONSE') {
          // Only replace chat messages if we explicitly requested a session switch and it matches exactly.
          if (data.request_id && data.request_id === pendingSessionSwitchRef.current) {
            pendingSessionSwitchRef.current = null; // Clear immediately to ignore any subsequent echoed broadcasts
            const sess = data.payload.session;
            setChatMessages(prev => {
              const backendMessages = (sess && sess.messages) ? sess.messages.map(m => {
                let parsedThoughts = m.thoughts;
                if (typeof parsedThoughts === 'string') {
                  try {
                    parsedThoughts = JSON.parse(parsedThoughts);
                  } catch (e) {
                    parsedThoughts = [];
                  }
                }
                return {
                  ...m,
                  role: m.role === 'assistant' ? 'agent' : m.role,
                  isFinal: true,
                  thoughts: Array.isArray(parsedThoughts) ? parsedThoughts : []
                };
              }) : [];
              
              // If we are currently running a task in this session, the DB won't have its incomplete messages yet.
              // We must merge it back from our cache or `prev` so the UI doesn't lose the stream.
              if (activeTaskSessionRef.current === activeSessionRef.current && isThinking) {
                 const activeMsg = prev.find(m => m.role === 'agent' && !m.isFinal);
                 if (activeMsg) {
                    return [...backendMessages, activeMsg];
                 }
              }
              return backendMessages;
            });
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
        } else if (data.type === 'MCP_RESOURCE_READ_RESPONSE') {
          // Issue 006: a resource loaded into the current chat appears
          // live as a system message (the engine also saved it to the
          // session, so it survives a reload). Only for our session —
          // a resource loaded into another session is not ours to show.
          if (data.payload.success && data.payload.message
              && data.payload.session_id === activeSessionRef.current) {
            setChatMessages(prev => [...prev, {
              id: data.request_id || Date.now().toString(),
              role: 'system',
              content: data.payload.message,
              isFinal: true
            }]);
          }
        } else if (data.type === 'MCP_PROMPT_RUN_RESPONSE') {
          // Issue 007: a prompt run into the current chat appears live,
          // one message per prompt message, with the prompt's own role.
          // Only for our session.
          if (data.payload.success && Array.isArray(data.payload.messages)
              && data.payload.session_id === activeSessionRef.current) {
            setChatMessages(prev => [...prev, ...data.payload.messages.map((m, i) => ({
              id: `${data.request_id || Date.now().toString()}-${i}`,
              role: m.role,
              content: m.content,
              isFinal: true
            }))]);
          }
        } else if (data.type === 'TOOL_APPROVAL_REQUEST') {
          setIsThinking(false);
          const approvalMsg = { 
            id: data.request_id || Date.now().toString(),
            role: 'tool_approval', 
            tool_name: data.payload.tool_name,
            tool_args: data.payload.tool_args || data.payload.args || {},
            approval_key: data.payload.approval_key || data.request_id,
            // Epic 2: engine-built preview ({diff} for edit_file, or
            // {error} when no preview could be built, or null).
            preview: data.payload.preview || null,
            isFinal: true
          };
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = [...cached, approvalMsg];
          } else {
            setChatMessages(prev => [...prev, approvalMsg]);
          }
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
          // A plan-mode choice made before the session existed (the
          // '__pending' key) is adopted by the new session.
          setPlanModeBySession(prev => {
            if (!prev.__pending) return prev;
            const next = { ...prev, [newId]: prev.__pending };
            delete next.__pending;
            return next;
          });
          // Same adoption for a scratchpad that arrived pre-session.
          setScratchpadBySession(prev => {
            if (!prev.__pending) return prev;
            const next = { ...prev, [newId]: prev.__pending };
            delete next.__pending;
            return next;
          });
          if (!data.payload?.preserve_messages) {
            setChatMessages([]);
          }
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
          // Phase 10.3 C1: providers is an ARRAY everywhere. The engine
          // sends a list; an object payload is normalised via Object.values
          // and a missing one becomes []. Never store an object here — the
          // old `|| {}` fallback is what made MODEL_DEFAULT_CHANGED below
          // silently no-op (its Array.isArray guard bailed out).
          const list = data.payload.providers;
          setProviders(Array.isArray(list) ? list : (list && typeof list === 'object' ? Object.values(list) : []));
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

    // Single subscription, via the facade. Registered synchronously so the
    // effect cleanup (StrictMode double-mount included) always removes it —
    // the old async initConnection leaked the first subscription, which is
    // what the deleted dedup hack was papering over.
    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }

    const unsubscribers = [];
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

    // Phase 9, B3: removed the 30 random "ambient" nodes that simulated a vast
    // network. The background graph is decorative branding, not live data —
    // it must not fabricate the appearance of a populated knowledge graph.
    return () => {
      clearInterval(memoryPoll);
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
      unsubscribers.forEach(unsub => {
        if (typeof unsub === 'function') unsub();
      });
    };
  }, []);

  // Phase 10.3 C1: connection transitions come from the facade's
  // isConnected (driven by its _ws_status listener and getWsStatus), which
  // replaces the _ws_status branch of App's old direct subscription. On
  // connect, (re-)request the lists the UI needs; on disconnect, stop any
  // spinner and surface the drop in chat.
  const wasConnectedRef = useRef(false);
  useEffect(() => {
    if (isConnected && !wasConnectedRef.current) {
      setStatus('Online');
      if (wsRef.current) {
        wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'PLUGIN_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
        wsRef.current.send(JSON.stringify({ type: 'SLASH_COMMAND_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
      }
    } else if (!isConnected && wasConnectedRef.current) {
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
    wasConnectedRef.current = isConnected;
  }, [isConnected, wsRef]);

  const handleSendMessage = (text) => {
    // One stable id for this message, shared by the rendered bubble and
    // the stored row (the engine saves it under this id), so a later
    // edit-and-resend addresses the exact message — never a content
    // lookalike. Falls back to a timestamp+random id where randomUUID
    // is unavailable.
    const messageId = (window.crypto && typeof window.crypto.randomUUID === 'function')
      ? window.crypto.randomUUID()
      : `msg-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
    // Epic 1, E1-07: sending while a task is running steers that task
    // instead of replacing it — the bubble is flagged queued until the
    // engine answers STEERING_APPLIED, and no new agent bubble starts.
    const steering = isThinking;
    // Epic 1, E1-09 UI: plan mode rides START_TASK only (a steer is
    // mid-task guidance, not a new task). Read the per-session choice.
    const planMode = !!planModeBySession[activeSessionRef.current || '__pending'];
    setChatMessages(prev => steering
      ? [...prev, { id: messageId, role: 'user', content: text, isFinal: true, queued: true, steering: true }]
      : [
          ...prev,
          { id: messageId, role: 'user', content: text, isFinal: true },
          { role: 'agent', content: '', thoughts: [], isFinal: false }
        ]);
    setIsThinking(true);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: steering ? 'STEER_TASK' : 'START_TASK',
        schema_version: 1,
        request_id: messageId,
        payload: {
          prompt: text,
          session_id: activeSession,
          message_id: messageId,
          ...(steering ? {} : { plan_mode: planMode })
        }
      }));
    } else if (steering) {
      // Offline steer: keep the bubble queued rather than failing the
      // running task's bubble; the engine never saw it, and clearing
      // the flag is STEERING_APPLIED's job once reconnect delivers it.
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
    setChatMessages(prev => {
      if (activeSessionRef.current) {
        sessionCacheRef.current[activeSessionRef.current] = prev;
      }
      return sessionCacheRef.current[id] || [];
    });
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
    setChatMessages([]); // Clear immediately for instant feel
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

  const handleToolApproval = (requestId, approved, approvalKey, scope) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const key = approvalKey || requestId;
      wsRef.current.send(JSON.stringify({
        type: approved ? 'TOOL_APPROVAL_GRANTED' : 'TOOL_APPROVAL_REJECTED',
        schema_version: 1,
        request_id: requestId,
        payload: {
          approval_key: key,
          approved: approved,
          // Epic 2, E2-09: a scoped grant ("file" / "folder") rides the
          // GRANTED event; a plain approve sends no scope (= "once").
          ...(approved && scope ? { scope } : {})
        }
      }));
      // Remove the approval card from chat
      setChatMessages(prev => prev.filter(m => m.id !== requestId && m.approval_key !== key));
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

  // Epic 1 UI batch: the truncate machinery shared by edit-and-resend
  // and regenerate (E1-08). sendSessionTruncate tells the engine to
  // wipe a user message + everything after it and returns the
  // occurrence of that message among identical user messages;
  // pruneMessagesFrom cuts the local list at the same row, resolved
  // defensively (rendered index, then shared id, then the
  // occurrence-aware content match) so the wrong branch is never cut.
  const sendSessionTruncate = (msg, index, list) => {
    const content = typeof msg.content === 'string' ? msg.content : '';
    let occurrence = 0;
    if (Number.isInteger(index) && index > 0) {
      occurrence = (list || [])
        .slice(0, index)
        .filter(m => m.role === 'user' && m.content === content)
        .length;
    }
    // Sent through the socket facade unconditionally: the facade
    // queues while offline and flushes in order on reconnect, so the
    // truncate is never silently dropped (a readyState check threw it
    // away whenever the socket was mid-reconnect). The occurrence
    // tells the engine WHICH identical message was addressed when the
    // id isn't a stored row id.
    const sessionId = activeSessionRef.current;
    if (sessionId && wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_TRUNCATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          session_id: sessionId,
          message_id: msg.id || null,
          content,
          role: 'user',
          occurrence
        }
      }));
    }
    return occurrence;
  };

  const pruneMessagesFrom = (prev, msg, index, occurrence) => {
    const content = typeof msg.content === 'string' ? msg.content : '';
    let idx = -1;
    if (Number.isInteger(index) && prev[index]
        && prev[index].role === 'user' && prev[index].content === content) {
      idx = index;
    }
    if (idx === -1 && msg.id) {
      idx = prev.findIndex(m => m.id === msg.id);
    }
    if (idx === -1) {
      let seen = -1;
      idx = prev.findIndex(m => {
        if (m.role === 'user' && m.content === content) {
          seen += 1;
          return seen === occurrence;
        }
        return false;
      });
    }
    return idx === -1 ? prev : prev.slice(0, idx);
  };

  const handleEditMessage = (msg, index) => {
    // Only user messages are editable, and only with real text content.
    if (!msg || msg.role !== 'user') return;

    // 1. Cancel any running task first so ghost progress doesn't stream in
    if (isThinking) {
      handleStopTask();
    }

    // 2+3. Truncate the stored session at this message and prune the
    // local chat from it onward; the composer refill happens in the
    // chat window (it owns the input state).
    const occurrence = sendSessionTruncate(msg, index, chatMessages);
    setChatMessages(prev => pruneMessagesFrom(prev, msg, index, occurrence));
  };

  // Epic 1 UI batch (E1-08): regenerate the last exchange. Same
  // truncate as edit-and-resend, but the prompt is resent immediately
  // as a fresh task — nothing goes into the composer, and the send
  // path adds exactly one new user bubble (the normal one).
  const handleRegenerate = () => {
    if (isThinking) return;
    const lastUserIdx = chatMessages.findLastIndex(m => m.role === 'user');
    if (lastUserIdx === -1) return;
    const userMsg = chatMessages[lastUserIdx];
    const prompt = typeof userMsg.content === 'string' ? userMsg.content : '';
    if (!prompt) return;
    const occurrence = sendSessionTruncate(userMsg, lastUserIdx, chatMessages);
    setChatMessages(prev => pruneMessagesFrom(prev, userMsg, lastUserIdx, occurrence));
    handleSendMessage(prompt);
  };

  // Epic 1, E1-09 UI: plan mode is a per-session choice.
  const planModeKey = activeSession || '__pending';
  const planMode = !!planModeBySession[planModeKey];
  const handleTogglePlanMode = () => {
    setPlanModeBySession(prev => ({ ...prev, [planModeKey]: !prev[planModeKey] }));
  };

  // Epic 1 UI batch (E1-08): session rename (inline edit in the list).
  const handleStartRenameSession = (session, e) => {
    if (e) e.stopPropagation();
    setEditingSessionId(session.id);
    setEditingSessionTitle(session.title || session.name || '');
  };

  const handleCancelRenameSession = () => {
    setEditingSessionId(null);
    setEditingSessionTitle('');
  };

  const handleCommitRenameSession = (sessionId) => {
    if (editingSessionId !== sessionId) return; // Escape-cancel already closed it
    const title = editingSessionTitle.trim();
    setEditingSessionId(null);
    setEditingSessionTitle('');
    if (!title) return; // an empty title cancels instead of renaming
    if (wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_RENAME_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { session_id: sessionId, title }
      }));
    }
  };

  // Epic 1 UI batch (E1-08): debounced session search. An empty query
  // restores the normal list; results are only applied when they answer
  // the current query (see SESSION_SEARCH_RESPONSE above).
  useEffect(() => {
    sessionSearchQueryRef.current = sessionSearchQuery;
    if (sessionSearchTimerRef.current) clearTimeout(sessionSearchTimerRef.current);
    if (!sessionSearchQuery.trim()) {
      setSessionSearchResults(null);
      return;
    }
    sessionSearchTimerRef.current = setTimeout(() => {
      if (wsRef.current && wsRef.current.send) {
        wsRef.current.send(JSON.stringify({
          type: 'SESSION_SEARCH_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { query: sessionSearchQuery }
        }));
      }
    }, 300);
    return () => {
      if (sessionSearchTimerRef.current) clearTimeout(sessionSearchTimerRef.current);
    };
  }, [sessionSearchQuery]);

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
        {/* Toggle Button for Sidebar */}
        {!isFloating && (
          <button
            onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)}
            style={{
              position: 'absolute',
              top: '22px',
              left: '24px',
              zIndex: 100,
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              color: '#e2e8f0',
              cursor: 'pointer',
              padding: '6px',
              borderRadius: '8px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transition: 'transform 160ms cubic-bezier(0.23, 1, 0.32, 1), background 160ms ease-out',
              backdropFilter: 'blur(8px)'
            }}
            onMouseDown={(e) => e.currentTarget.style.transform = 'scale(0.95)'}
            onMouseUp={(e) => e.currentTarget.style.transform = 'scale(1)'}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.background = 'rgba(255,255,255,0.05)';
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(255,255,255,0.1)'}
          >
            <Menu size={20} />
          </button>
        )}

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
            zIndex: 20,
            transform: isSidebarCollapsed ? 'translateX(-100%)' : 'translateX(0)',
            transition: 'transform 250ms cubic-bezier(0.23, 1, 0.32, 1)'
          }}>
            <div style={{ padding: '0 24px', paddingLeft: '72px', marginBottom: '32px', display: 'flex', alignItems: 'center', gap: '12px' }}>
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
              
              {/* Epic 1 UI batch (E1-08): session search. A non-empty
                  query swaps the list for engine search results. */}
              <div style={{ padding: '0 24px', marginBottom: '12px' }}>
                <input
                  type="text"
                  placeholder="Search chats..."
                  aria-label="Search chats"
                  value={sessionSearchQuery}
                  onChange={(e) => setSessionSearchQuery(e.target.value)}
                  style={{
                    width: '100%',
                    boxSizing: 'border-box',
                    background: 'rgba(255,255,255,0.05)',
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: '8px',
                    padding: '7px 10px',
                    color: '#fff',
                    fontSize: '12px',
                    outline: 'none'
                  }}
                />
              </div>

              <div className="chat-scroll" style={{ padding: '0 12px', flex: 1, overflowY: 'auto' }}>
                {sessionSearchResults !== null ? (
                  sessionSearchResults.length === 0 ? (
                    <div style={{ color: '#9090a0', fontSize: '13px', padding: '12px 14px' }}>No chats match your search.</div>
                  ) : sessionSearchResults.map(r => (
                    <div
                      key={r.id}
                      className={`session-item ${activeSession === r.id ? 'active' : ''}`}
                      onClick={() => handleSessionSwitch(r.id)}
                      style={{
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: '8px'
                      }}
                    >
                      <MessageSquare size={16} style={{ opacity: activeSession === r.id ? 1 : 0.5, flexShrink: 0, marginTop: '2px' }} />
                      <div style={{ overflow: 'hidden' }}>
                        <div style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.title || `Session ${r.id}`}</div>
                        {r.snippet && (
                          <div style={{ fontSize: '12px', color: '#9090a0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</div>
                        )}
                      </div>
                    </div>
                  ))
                ) : sessions.map(s => (
                  <div
                    key={s.id}
                    className={`session-item ${activeSession === s.id ? 'active' : ''}`}
                    onClick={() => handleSessionSwitch(s.id)}
                    onDoubleClick={() => handleStartRenameSession(s)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', overflow: 'hidden', flex: 1 }}>
                      <MessageSquare size={16} style={{ opacity: activeSession === s.id ? 1 : 0.5, flexShrink: 0 }} />
                      {editingSessionId === s.id ? (
                        <input
                          type="text"
                          aria-label="Session title"
                          autoFocus
                          value={editingSessionTitle}
                          onChange={(e) => setEditingSessionTitle(e.target.value)}
                          onClick={(e) => e.stopPropagation()}
                          onDoubleClick={(e) => e.stopPropagation()}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              handleCommitRenameSession(s.id);
                            } else if (e.key === 'Escape') {
                              e.preventDefault();
                              handleCancelRenameSession();
                            }
                          }}
                          onBlur={() => handleCommitRenameSession(s.id)}
                          style={{
                            flex: 1,
                            minWidth: 0,
                            background: 'rgba(0,0,0,0.3)',
                            border: '1px solid rgba(167, 139, 250, 0.5)',
                            borderRadius: '6px',
                            padding: '3px 6px',
                            color: '#fff',
                            fontSize: '13px',
                            outline: 'none'
                          }}
                        />
                      ) : (
                        <span style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title || s.name || `Session ${s.id}`}</span>
                      )}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
                      <button
                        className="session-rename-btn"
                        onClick={(e) => handleStartRenameSession(s, e)}
                        title="Rename Session"
                        aria-label="Rename session"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="session-delete-btn"
                        onClick={(e) => handleDeleteSession(s.id, e)}
                        title="Delete Session"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
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
          onEditMessage={handleEditMessage}
          onRegenerate={handleRegenerate}
          planMode={planMode}
          onTogglePlanMode={handleTogglePlanMode}
          scratchpad={scratchpadBySession[activeSession || '__pending'] || null}
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
          slashCommands={slashCommands}
          isSidebarCollapsed={isSidebarCollapsed}
        />
      )}

      {activeTab !== 'Chat' && (
        <div style={{
          position: 'absolute',
          top: '40px',
          left: isSidebarCollapsed ? '80px' : '360px',
          right: '40px',
          bottom: '40px',
          zIndex: 40,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          transition: 'left 250ms cubic-bezier(0.23, 1, 0.32, 1)'
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
                   activeSession={activeSession}
                 />
               </div>
            )}
            {activeTab === 'Dashboard' && <Dashboard status={status} activeTasks={activeTaskIds.size} pluginCount={pluginCount} memoryMb={memoryMb} onStartWorkflow={() => setActiveTab('Chat')} />}
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
            graphData={initGraphData}
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

export default function App() {
  return (
    <SocketProvider>
      <AppContent />
    </SocketProvider>
  );
}
