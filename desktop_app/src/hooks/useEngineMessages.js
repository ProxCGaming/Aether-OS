import { useEffect } from 'react';
import { groupEntriesByTask } from '../components/CostHeaderPopover';

// Wave 5, Annex C row 5 (message-router seam): the once-registered
// engine-message handler, extracted from App.jsx. App owns the state;
// this hook owns the dispatch. It is handed the refs, state setters
// and stable callbacks the dispatch writes through — the handler
// reads CURRENT values through refs (the D1-A1 discipline), never
// render-scope state, because the subscription is registered once
// and its closure would otherwise freeze on mount-time values.
//
// The two D3-19 callbacks are the one deliberate behaviour addition
// in this extraction: App runs the artifact-content loading
// lifecycle, and the handler reports the two load events only it
// can see (a content request going out for the restored canvas
// selection, and a content response landing).
//
// The USAGE_LEDGER_QUERY_RESPONSE branch groups ledger entries
// with CostHeaderPopover's groupEntriesByTask — imported here
// directly (its only remaining consumer) rather than threaded
// through the deps.

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

export function useEngineMessages(deps) {
  const {
    wsRef, isFloating, initialSession,
    activeSessionRef, activeTaskSessionRef, browserSessionIdsRef,
    chatMessagesRef, isThinkingRef, moveBatchRef, pendingApprovalsRef,
    pendingSessionSwitchIdRef, pendingSessionSwitchRef, pendingThinkingRef,
    readAloudAudioCtxRef, readAloudPlayHeadRef, readAloudSourcesRef,
    rewindUndoTimerRef, sessionCacheRef, sessionSearchQueryRef, sessionsRef,
    voiceChunkSeqRef, voiceRtcSeqRef, voiceStateRef,
    setActiveArtifactContent, setActiveSession, setActiveTaskIds,
    setBrowserSessions, setBudgetAlert, setCanvasArtifacts, setChatMessages,
    setContextBreakdown, setCostTasks, setDailyBudget, setDesktopControl,
    setDevServerInfo,
    setDictationState, setEngineConfig, setExportStatus, setImportStatus,
    setIsThinking, setLastCostTaskId, setLocalModels, setMoveStatus,
    setPlanModeBySession, setPluginCount, setProviderHealth,
    setProviderHealthLoaded, setProviders, setProvidersLoaded,
    setReadAloudState, setRewindConfirm, setRewindManifest, setRewindUndo,
    setScratchpadBySession, setSelectedArtifactId, setSelectedSessionIds,
    setSessionSearchResults, setSessionSelectMode, setSessionSwitchPendingId,
    setSessions, setSlashCommands, setStatus, setVoiceAudioChunk,
    setVoicePlaybackCut, setVoiceRtcAnswer, setVoiceRtcRemoteCandidate,
    setWorkspaces,
    applyVoiceState, clearRewindUndo, commitVoiceAssistant,
    handleRewindUndoExpire, handleSessionSwitch, requestContextBreakdown,
    requestCostBreakdown, requestRewindManifest, requestWorkspaceList,
    sendSessionListRequest, stopAppReadAloud,
    onArtifactLoadStart, onArtifactLoadEnd
  } = deps;

  // Phase 10.3 C1: subscribe to engine messages through the single
  // SocketContext facade (wsRef). No mock wsRef, no direct onEngineMessage
  // subscription, no local dedup — all of that lives in the facade now.
  useEffect(() => {
    // Vault fill approvals (F2 fix): the engine fails an
    // ask-every-time fill closed on its own 120s timeout, so a vault
    // prompt still undecided when its task ends can never be
    // answered live. The tool-approval flow only disarms the
    // shortcut ref at task end (D1-A5); the vault card additionally
    // gets a visual expired state, because its timeout can lapse
    // mid-task (the fill tool returns the refusal and the task
    // moves on). A stale answer stays harmless regardless — the
    // engine's resolve_fill_approval returns False for unknown ids
    // and the WS handler tolerates it silently.
    const expireVaultFillCards = (list) => list.map(m =>
      (m.role === 'vault_fill_approval' && !m.decisionQueued && !m.expired)
        ? { ...m, expired: true }
        : m);
    const expirePendingVaultFillApprovals = () => {
      if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
        const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
        sessionCacheRef.current[activeTaskSessionRef.current] = expireVaultFillCards(cached);
      } else {
        setChatMessages(prev => expireVaultFillCards(prev));
      }
    };
    // S2 (master build): a system line routed to the session that
    // owns it — visible chat when that session is on screen, the
    // session cache otherwise (the TOOL_APPROVAL_REQUEST routing
    // pattern, for status pushes that carry their own session id).
    const appendSystemMessage = (sessionId, content, idPrefix) => {
      const systemMsg = {
        id: `${idPrefix}-${Date.now()}`,
        role: 'system',
        content,
        isFinal: true,
      };
      if (sessionId && sessionId !== activeSessionRef.current) {
        const cached = sessionCacheRef.current[sessionId] || [];
        sessionCacheRef.current[sessionId] = [...cached, systemMsg];
      } else {
        setChatMessages(prev => [...prev, systemMsg]);
      }
    };
    const handleMessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        // S3-N1: one malformed frame must not kill the central
        // message handler — log and drop it (the SocketContext
        // facade's parse pattern, applied at this layer too).
        console.warn('Dropping malformed engine message');
        return;
      }
        if (data.type === 'hello') {
          setStatus(`Online / ${data.payload?.active_model || 'Agent'}`);
        } else if (data.type === 'TASK_CREATED') {
          // E6-F10: TASK_CREATED is the start event the engine
          // actually emits (graph + fast-chat runners); TASK_STARTED
          // is never sent, so counting only the branch below left
          // the Dashboard's Active Tasks at 0 for real tasks. A
          // created task counts until its terminal event (handled by
          // the TASK_COMPLETED / TASK_FAILED / TASK_CANCELLED branch,
          // which deletes the same id). Counting only — the
          // streaming-bubble lifecycle below keys off the client's
          // own START_TASK echo, unchanged.
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => new Set(prev).add(data.payload?.task_id || data.request_id));
          }
        } else if (data.type === 'START_TASK' || data.type === 'TASK_STARTED') {
          // Track active task for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => new Set(prev).add(data.payload?.task_id || data.request_id));
          }
          activeTaskSessionRef.current = data.payload?.session_id || activeSessionRef.current;
          // E8-1.1a: a new task supersedes the session's manifest;
          // fetch now (points land as it runs) and again at the
          // terminal event, which carries the complete trail.
          requestRewindManifest(activeTaskSessionRef.current);
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
              // D1-A11: no side effects inside a state updater
              // (StrictMode double-invokes them, which double-appended
              // the buffered delta). The decision is made against the
              // ref mirror of the visible list; the mirror is updated
              // synchronously so back-to-back deltas compose.
              const updated = applyThinking(chatMessagesRef.current);
              if (updated) {
                chatMessagesRef.current = updated;
                setChatMessages(updated);
              } else {
                pendingThinkingRef.current[bufferKey] = appendThoughtDelta(pendingThinkingRef.current[bufferKey] || [], thinkNode, thinkDelta);
              }
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
          let delta = data.payload?.text_delta;
          if (delta !== undefined && delta !== null && typeof delta !== 'string') {
            delta = JSON.stringify(delta, null, 2);
          }
          const node = data.payload?.node;
          const isThought = node && node !== 'assistant' && node !== 'agent';

          // S12: no text sniffing here any more. Delegation used to be
          // filtered out of the thought stream by matching the
          // '[Supervisor] -> Delegating' prefix; since S12 the engine
          // never emits delegation as text — it arrives only as the
          // structured NODE_ACTIVITY event, so every TASK_PROGRESS
          // delta is genuine content and renders as it arrives.

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
                // D1-A6: results land in call order, so a result
                // belongs to the FIRST still-pending call of this
                // tool — the previous findLastIndex handed it to the
                // LAST one and cross-wired the arguments of
                // overlapping same-tool calls (parallel read_files
                // are routine).
                const pendingIdx = updatedThoughts.findIndex(t =>
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
          // D1-A5: approvals belong to the task that requested them;
          // once it ends they must stop being shortcut-actionable
          // (the Ctrl+Enter/Ctrl+Shift+Enter listener reads this ref).
          pendingApprovalsRef.current = [];
          // F2: an undecided vault fill prompt is engine-dead now —
          // mark the card expired, not just disarmed.
          expirePendingVaultFillApprovals();
          // E8-1.1a: the finished task's manifest is complete now.
          requestRewindManifest(data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current);
          // E8-A8.1a: reconcile the cost surfaces with the ledger
          // now the task's calls are all recorded (also covers the
          // fast-chat path, whose calls publish no live bus events).
          requestCostBreakdown(data.payload?.session_id || activeSessionRef.current);
          requestContextBreakdown(data.payload?.session_id || activeSessionRef.current);
          // Remove from active tasks for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          if (data.payload?.session_id && !activeSessionRef.current) {
            setActiveSession(data.payload.session_id);
            if (wsRef.current) {
              sendSessionListRequest();
        wsRef.current.send(JSON.stringify({ type: 'CONFIG_GET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          }
          let result = data.payload.response || data.payload.result || data.payload.output;

          // E1-16: THINKING deltas still buffered (no streaming bubble
          // ever appeared) join the final message's thoughts here —
          // reasoning is preserved, and still kept out of content.
          const completionKey = activeTaskSessionRef.current || data.payload?.session_id || activeSessionRef.current || '__pending';
          const bufferedThoughts = pendingThinkingRef.current[completionKey] || [];
          if (bufferedThoughts.length) delete pendingThinkingRef.current[completionKey];

          // C6 (Wave 6): the memory items this reply used, as the
          // engine disclosed them on the completion payload of BOTH
          // chat paths (supervisor task and fast chat both settle
          // through this branch) — summaries only, engine-capped.
          // The id key is spelled fact_id in the contract text and
          // id on the wire; normalise here so the transcript never
          // has to care. Items without a summary are dropped: there
          // is nothing honest to show for them. Empty/absent leaves
          // the field off the message entirely, and the transcript
          // renders no affordance for it.
          const rawMemoryUsed = Array.isArray(data.payload?.memory_used)
            ? data.payload.memory_used : [];
          const memoryUsed = rawMemoryUsed
            .map((item) => (item && typeof item === 'object' ? {
              id: item.id ?? item.fact_id ?? null,
              summary: typeof item.summary === 'string' ? item.summary : '',
              kind: typeof item.kind === 'string' ? item.kind : 'fact',
            } : null))
            .filter((item) => item && item.summary);
          const memoryField = memoryUsed.length ? { memory_used: memoryUsed } : {};

          const updateCompletedMessages = (prev) => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : last.content;
              const baseThoughts = (last.thoughts && last.thoughts.length > 0) ? last.thoughts : (data.payload.thoughts || []);
              const finalThoughts = bufferedThoughts.length ? mergeThoughtEntries(baseThoughts, bufferedThoughts) : baseThoughts;
              
              newMessages[lastAgentIdx] = { ...last, content: finalContent, thoughts: finalThoughts, isFinal: true, ...memoryField };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : '';
              const createdThoughts = bufferedThoughts.length ? mergeThoughtEntries(data.payload.thoughts || [], bufferedThoughts) : (data.payload.thoughts || []);
              return [...newMessages, { role: 'agent', content: finalContent, thoughts: createdThoughts, isFinal: true, ...memoryField }];
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
          // D1-A5: see TASK_COMPLETED — a dead task's approvals must
          // not stay armed for the keyboard shortcuts.
          pendingApprovalsRef.current = [];
          // F2: see TASK_COMPLETED — expire undecided vault cards.
          expirePendingVaultFillApprovals();
          // E8-1.1a: failed/cancelled tasks still recorded points.
          requestRewindManifest(data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current);
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
        } else if (data.type === 'TASK_INTERRUPTED') {
          // S2 (master build): the executor emits this when a task
          // pauses for approval (state PENDING_APPROVAL) — it is NOT
          // a terminal event: the approval card (TOOL_APPROVAL_REQUEST,
          // which arrives first) stays armed in pendingApprovalsRef,
          // the streaming bubble stays open so the resumed run can
          // stream into it, and the task stays in activeTaskIds until
          // its real terminal event. What was missing before this
          // branch: nothing in the transcript said the task had
          // paused, so the pause was only visible as the approval
          // card itself — and a push arriving without its card (e.g.
          // after a reconnect) left a stale running state behind.
          setIsThinking(false);
          appendSystemMessage(
            data.payload?.session_id || activeTaskSessionRef.current,
            '⏸ Task paused — waiting for your approval to continue.',
            'task-interrupted');
        } else if (data.type === 'SESSION_RENAME_RESPONSE') {
          // Epic 1 UI batch (E1-08): the engine confirms renames; only
          // a confirmed rename changes the local list.
          if (data.payload?.renamed && data.payload.session_id) {
            setSessions(prev => prev.map(s => s.id === data.payload.session_id ? { ...s, title: data.payload.title } : s));
          }
        } else if (data.type === 'SESSION_TRUNCATE_RESPONSE') {
          // S2 (master build): the answer to SESSION_TRUNCATE_REQUEST
          // (edit-and-resend / regenerate). The local list is pruned
          // optimistically at send time, so a confirmed truncate needs
          // no further change — but a REFUSED one (truncated: false)
          // means the stored session still holds the pruned branch
          // and it would reappear on the next reload. Before this
          // branch that answer was unheard and the divergence was
          // invisible; now the refusal is surfaced where it happened.
          if (data.payload && data.payload.truncated === false) {
            appendSystemMessage(
              data.payload.session_id || activeSessionRef.current,
              '⚠️ The engine could not truncate this conversation — the messages you removed are still in the stored session and may reappear when it is reloaded.',
              'session-truncate-failed');
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
          // D1-A9: a payload-less frame must not throw here — the
          // dispatch of every later message depends on this listener
          // surviving malformed-but-parseable frames.
          const sessionList = data.payload?.sessions || [];
          setSessions(sessionList);
          if (sessionList.length > 0 && !activeSessionRef.current) {
            handleSessionSwitch(sessionList[0].id);
          } else if (isFloating && initialSession) {
            handleSessionSwitch(initialSession);
          }
        } else if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          // Per-workspace sessions: App keeps its own copy of the
          // workspace rows — the session list filters by the active
          // row (see the effect below) and move menus/search labels
          // read names from it. Projects requests the same list on
          // the shared socket, so these replies also land here.
          setWorkspaces(Array.isArray(data.payload?.workspaces) ? data.payload.workspaces : []);
        } else if (data.type === 'WORKSPACE_SET_ACTIVE_RESPONSE') {
          // The active flag lives on the LIST rows; refetch them so
          // the workspace-change effect re-files the session list.
          if (data.payload?.success) requestWorkspaceList();
        } else if (data.type === 'SESSION_MOVE_RESPONSE') {
          // Per-workspace sessions (Q6): move outcomes. A successful
          // move re-files the row in place — its new binding rides
          // the reply; a refusal leaves the row exactly as it was.
          // Bulk moves settle one reply at a time into moveBatchRef;
          // the last reply reconciles the filtered list and reports
          // the tally honestly (n moved, m failed + first reason).
          const p = data.payload || {};
          if (p.moved && p.session_id) {
            setSessions(prev => prev.map(s => s.id === p.session_id ? { ...s, workspace: p.workspace ?? null } : s));
          }
          const batch = moveBatchRef.current;
          if (batch) {
            batch.settled += 1;
            if (p.moved) {
              batch.moved += 1;
            } else {
              batch.failed += 1;
              if (!batch.firstError) batch.firstError = p.error || 'Move refused';
            }
            if (batch.settled >= batch.total) {
              moveBatchRef.current = null;
              if (batch.total > 1) {
                setSessionSelectMode(false);
                setSelectedSessionIds([]);
                setMoveStatus(batch.failed > 0
                  ? { type: 'error', message: `${batch.moved} moved, ${batch.failed} failed: ${batch.firstError}` }
                  : { type: 'success', message: `${batch.moved} sessions moved.` });
              } else if (batch.failed > 0) {
                setMoveStatus({ type: 'error', message: batch.firstError });
              }
              sendSessionListRequest();
            }
          }
        } else if (data.type === 'SLASH_COMMAND_LIST_RESPONSE') {
          setSlashCommands(data.payload.commands || []);
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE' || data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'SLASH_COMMAND_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            wsRef.current.send(JSON.stringify({ type: 'SKILL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            // Per-workspace sessions: keep App's workspace rows (and
            // therefore the session list filter) current as roots
            // come and go.
            requestWorkspaceList();
          }
        } else if (data.type === 'SESSION_GET_RESPONSE') {
          // Only replace chat messages if we explicitly requested a session switch and it matches exactly.
          // Wave 2 slice 2 (RUX-A-01): a switch sent via safeSend has a
          // facade-minted request_id, so it registers its target in
          // pendingSessionSwitchIdRef instead; a reply whose session
          // matches that target is honoured the same way. Replies for
          // any other session are still ignored (echoed broadcasts
          // cannot hijack the view).
          const switchByRequest = data.request_id && data.request_id === pendingSessionSwitchRef.current;
          const switchBySession = pendingSessionSwitchIdRef.current !== null
            && data.payload?.session?.id === pendingSessionSwitchIdRef.current;
          if (switchByRequest || switchBySession) {
            pendingSessionSwitchRef.current = null; // Clear immediately to ignore any subsequent echoed broadcasts
            pendingSessionSwitchIdRef.current = null;
            setSessionSwitchPendingId(null);
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
              // D1-A1: `isThinking` here was the mount-time render
              // value (always false) inside this once-registered
              // handler, so the stranded bubble was silently dropped;
              // the live value comes from the ref mirror.
              if (activeTaskSessionRef.current === activeSessionRef.current && isThinkingRef.current) {
                 const activeMsg = prev.find(m => m.role === 'agent' && !m.isFinal);
                 if (activeMsg) {
                    return [...backendMessages, activeMsg];
                 }
              }
              return backendMessages;
            });
            // E8-1.1a: surface the switched-to session's points.
            requestRewindManifest(sess?.id || activeSessionRef.current);
          }
        } else if (data.type === 'REWIND_MANIFEST_GET_RESPONSE') {
          // E8-1.1a: only the active session's manifest is surfaced;
          // an empty trail clears it (the step menus stay hidden).
          if (data.payload?.session_id === activeSessionRef.current) {
            const entries = Array.isArray(data.payload.entries) ? data.payload.entries : [];
            setRewindManifest(entries.length ? {
              session_id: data.payload.session_id,
              task_id: data.payload.task_id,
              entries,
            } : null);
          }
        } else if (data.type === 'TASK_REWIND_RESPONSE') {
          // E8-1.1b: the engine rolled the session back. M12: render
          // the retained history the ENGINE returns in the response —
          // its own record of what the rewind kept. The old local
          // structural cut diverged at the final point (the engine
          // discards no stored messages there, but the local cut
          // removed the response anyway). The structural cut remains
          // only as a fallback for responses without the history.
          // Then arm the undo window.
          if (data.payload?.session_id === activeSessionRef.current) {
            // Wave 2 slice 2 (RUX-A-03): a rewind confirmed while
            // offline kept its modal open in the queued state; the
            // flushed request has now actually landed, so close it.
            setRewindConfirm(null);
            if (data.payload.ok) {
              if (Array.isArray(data.payload.messages)) {
                setChatMessages(data.payload.messages.map(m => {
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
                }));
              } else {
                const step = data.payload.rewound_to_step;
                setChatMessages(prev => {
                  let taskStart = -1;
                  prev.forEach((m, j) => {
                    if (m && m.role === 'user' && !m.steering) taskStart = j;
                  });
                  if (taskStart === -1) return prev;
                  return step === 0 ? prev.slice(0, taskStart) : prev.slice(0, taskStart + 1);
                });
              }
              requestRewindManifest(data.payload.session_id);
              const expiresAt = (data.payload.undo_expires_at || 0) * 1000;
              setRewindUndo({ sessionId: data.payload.session_id, expiresAt });
              if (rewindUndoTimerRef.current) clearTimeout(rewindUndoTimerRef.current);
              rewindUndoTimerRef.current = setTimeout(() => {
                handleRewindUndoExpire();
              }, Math.max(0, expiresAt - Date.now()));
            } else if (data.payload.error) {
              // D1-A8: a refusal the user caused must be visible
              // where they caused it — a system line in the chat,
              // not only a console warning.
              setChatMessages(prev => [...prev, {
                id: `rewind-refused-${Date.now()}`,
                role: 'system',
                content: `Rewind refused: ${data.payload.error}`,
                isFinal: true,
              }]);
            }
          }
        } else if (data.type === 'TASK_REWIND_UNDO_RESPONSE') {
          // E8-1.1b: the discarded steps are back in the store; the
          // rendered list is rebuilt from it via a session fetch
          // (the SESSION_GET_RESPONSE handler owns that rebuild and
          // only honours the request id registered here).
          if (data.payload?.ok && data.payload?.restored) {
            const sessionId = activeSessionRef.current;
            clearRewindUndo();
            if (sessionId && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              const reqId = `rewind_restore_${Date.now()}`;
              pendingSessionSwitchRef.current = reqId;
              wsRef.current.send(JSON.stringify({
                type: 'SESSION_GET_REQUEST',
                schema_version: 1,
                request_id: reqId,
                payload: { session_id: sessionId }
              }));
              requestRewindManifest(sessionId);
            }
          } else {
            clearRewindUndo();
          }
        } else if (data.type === 'BROWSER_TASK_PAUSE_RESPONSE') {
          // Epic 8, E8-6.1: dedicated-profile agent browser control response.
          // State keys by the owning CHAT session id (payload's
          // chat_session_id); the browser session id is remembered
          // for exact addressing of later control messages (M04).
          const payload = data.payload || {};
          const sid = payload.chat_session_id || activeSessionRef.current;
          if (sid && payload.session_id) {
            browserSessionIdsRef.current[sid] = payload.session_id;
          }
          if (sid) {
            setBrowserSessions(prev => ({
              ...prev,
              [sid]: {
                controlState: payload.control_state,
                currentUrl: payload.current_url || '',
                challenge: payload.challenge || null,
                browserSessionId: payload.session_id || null,
              }
            }));
          }
        } else if (data.type === 'DESKTOP_CONTROL_RESPONSE') {
          // Master build S20: desktop control lane state. Unlike
          // the browser's per-session sessions, the desktop lane
          // is machine-wide (one desktop), so the state is a
          // single value, not keyed by chat session.
          const payload = data.payload || {};
          if (payload.success !== false) {
            setDesktopControl({
              controlState: payload.control_state || 'agent',
              stoppedTasks: payload.stopped_tasks || [],
              drivingTask: payload.driving_task || null,
            });
          }
        } else if (data.type === 'BROWSER_CHALLENGE_DETECTED') {
          // Epic 8, E8-6.1: CAPTCHA / 2FA park notification — agent never solves challenges.
          // Same keying as the control response above (M04).
          const payload = data.payload || {};
          const sid = payload.chat_session_id || activeSessionRef.current;
          if (sid && payload.session_id) {
            browserSessionIdsRef.current[sid] = payload.session_id;
          }
          if (sid) {
            setBrowserSessions(prev => ({
              ...prev,
              [sid]: {
                controlState: 'waiting_user',
                currentUrl: payload.url || '',
                challenge: { challenge: payload.challenge, url: payload.url },
                browserSessionId: payload.session_id || null,
              }
            }));
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
          // E6-F13: track for the approve/reject shortcuts.
          pendingApprovalsRef.current = [
            ...pendingApprovalsRef.current,
            {
              kind: 'tool',
              id: data.request_id || Date.now().toString(),
              approval_key: data.payload.approval_key || data.request_id,
            },
          ];
          const approvalMsg = {
            id: data.request_id || Date.now().toString(),
            role: 'tool_approval', 
            tool_name: data.payload.tool_name,
            tool_args: data.payload.tool_args || data.payload.args || {},
            approval_key: data.payload.approval_key || data.request_id,
            // Epic 2: engine-built preview ({diff} for edit_file, or
            // {error} when no preview could be built, or null).
            preview: data.payload.preview || null,
            // Epic 8, E8-6.2: browser confirmation details (category
            // + the exact action named from the page) when this
            // approval is a browser confirmation; null otherwise.
            browser_confirmation: data.payload.browser_confirmation || null,
            // Master build S20: desktop confirmation details
            // (category + the exact action named from the resolved
            // element) when this approval is a desktop actuation
            // confirmation; null otherwise.
            desktop_confirmation: data.payload.desktop_confirmation || null,
            isFinal: true
          };
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = [...cached, approvalMsg];
          } else {
            setChatMessages(prev => [...prev, approvalMsg]);
          }
        } else if (data.type === 'PLUGIN_APPROVAL_REQUEST') {
          // D4-4: an install started from the Plugins panel mints a
          // `settings-install-…` request id and the panel claims the
          // approval itself, rendering the shared trust card there.
          // Minting a chat card too would put two live approve
          // surfaces on one decision — the panel owns this one.
          if (typeof data.request_id === 'string' && data.request_id.startsWith('settings-install-')) {
            return;
          }
          setIsThinking(false);
          // E6-F13: track for the approve/reject shortcuts.
          pendingApprovalsRef.current = [
            ...pendingApprovalsRef.current,
            { kind: 'plugin', id: data.request_id || Date.now().toString() },
          ];
          setChatMessages(prev => [...prev, {
            id: data.request_id || Date.now().toString(),
            role: 'plugin_approval',
            plugin_name: data.payload.plugin_name,
            plugin_details: data.payload.plugin_details,
            isFinal: true
          }]);
        } else if (data.type === 'VAULT_FILL_APPROVAL_REQUEST') {
          // F2 (post-Wave-6 linkage audit): the desktop half of the
          // vault ask-every-time round trip. The engine's vault
          // service publishes this on the task's event bus with
          // payload {request_id, entry_id, site, username, task_id}
          // and waits for VAULT_FILL_APPROVAL_RESPONSE (contracts.py)
          // — before this branch nothing in the desktop answered, so
          // every ask-every-time fill timed out and was refused.
          // Mirrors TOOL_APPROVAL_REQUEST: a transcript card routed
          // to the owning task's session, tracked for the E6-F13
          // shortcuts, answered from App (handleVaultFillApproval).
          // The payload never carries a password — site + username.
          setIsThinking(false);
          const vaultPayload = data.payload || {};
          const vaultRequestId = data.request_id || vaultPayload.request_id || Date.now().toString();
          pendingApprovalsRef.current = [
            ...pendingApprovalsRef.current,
            { kind: 'vault', id: vaultRequestId },
          ];
          const vaultMsg = {
            id: vaultRequestId,
            role: 'vault_fill_approval',
            entry_id: vaultPayload.entry_id || null,
            site: vaultPayload.site || '',
            username: vaultPayload.username || '',
            task_id: vaultPayload.task_id || null,
            isFinal: true
          };
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = [...cached, vaultMsg];
          } else {
            setChatMessages(prev => [...prev, vaultMsg]);
          }
        } else if (data.type === 'SESSION_CREATE_RESPONSE') {
          const newId = data.payload.session_id;
          // D1-A5: a fresh session orphans the previous task's
          // undecided approvals — they must not stay armed.
          pendingApprovalsRef.current = [];
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
            sendSessionListRequest();
        wsRef.current.send(JSON.stringify({ type: 'CONFIG_GET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
          }
        } else if (data.type === 'SESSION_DELETE_RESPONSE') {
          const deletedId = data.payload?.session_id;
          // D1-A1/A2: the old branch read the mount-time
          // `activeSession` (so deleting the viewed session never
          // switched away from it) and ran the switch plus further
          // setState calls INSIDE the setSessions updater — updaters
          // must be pure; StrictMode double-invokes them, which fired
          // the switch and its socket send twice. Compute from the
          // ref mirrors, set the list, then act.
          const next = sessionsRef.current.filter(s => s.id !== deletedId);
          setSessions(next);
          if (activeSessionRef.current === deletedId) {
            if (next.length > 0) {
              handleSessionSwitch(next[0].id);
            } else {
              setActiveSession(null);
              setChatMessages([]);
            }
          }
        } else if (data.type === 'SESSION_EXPORT_RESPONSE') {
          if (data.payload?.error) {
            setExportStatus({ type: 'error', message: `Export failed: ${data.payload.error}` });
          } else {
            if (data.payload?.content) {
              const mime = data.payload.format === 'markdown' ? 'text/markdown;charset=utf-8' : 'application/json;charset=utf-8';
              const blob = new Blob([data.payload.content], { type: mime });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = data.payload.filename || `session.${data.payload.format === 'markdown' ? 'md' : 'json'}`;
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              URL.revokeObjectURL(url);
            }
            setExportStatus({
              type: 'success',
              message: `Exported ${data.payload?.filename || data.payload?.format || 'session'}`
            });
          }
        } else if (data.type === 'SESSION_FORK_RESPONSE') {
          // E8-1.2: the fork is a new session ("Fork of <title>")
          // holding the history up to the chosen step. Refresh the
          // list and land the user in it; the source is untouched.
          if (data.payload?.ok && data.payload?.new_session_id) {
            sendSessionListRequest();
            handleSessionSwitch(data.payload.new_session_id);
          } else if (data.payload?.error) {
            // D1-A8: same rule as the rewind refusal — visible in
            // the chat the user is looking at, not console-only.
            setChatMessages(prev => [...prev, {
              id: `fork-refused-${Date.now()}`,
              role: 'system',
              content: `Fork refused: ${data.payload.error}`,
              isFinal: true,
            }]);
          }
        } else if (data.type === 'SESSION_IMPORT_RESPONSE') {
          if (data.payload?.ok) {
            sendSessionListRequest();
            if (data.payload.session_id) {
              handleSessionSwitch(data.payload.session_id);
            }
            setImportStatus({ type: 'success', message: `Restored session: ${data.payload.title}` });
          } else {
            setImportStatus({ type: 'error', message: data.payload?.error || 'Failed to import session' });
          }
        } else if (data.type === 'PROVIDER_LIST_RESPONSE') {
          // sends a list; an object payload is normalised via Object.values
          // and a missing one becomes []. Never store an object here — the
          // old `|| {}` fallback is what made MODEL_DEFAULT_CHANGED below
          // silently no-op (its Array.isArray guard bailed out).
          const list = data.payload.providers;
          setProviders(Array.isArray(list) ? list : (list && typeof list === 'object' ? Object.values(list) : []));
          setProvidersLoaded(true);
          // Epic 5, E5-05: per-provider health travels with the list.
          if (data.payload.health && typeof data.payload.health === 'object') {
            setProviderHealth(data.payload.health);
            setProviderHealthLoaded(true);
          }
        } else if (data.type === 'REFRESH_MODELS_RESPONSE') {
          // S21: the refresh answer carries the same provider
          // list shape as PROVIDER_LIST_RESPONSE (the engine
          // rebuilds the list after re-discovery) — apply it the
          // same way, or a completed refresh would change
          // nothing on screen and the feature would still be
          // dead from the user. It carries no health field, so
          // provider health is left as the list last reported it.
          const list = data.payload.providers;
          setProviders(Array.isArray(list) ? list : (list && typeof list === 'object' ? Object.values(list) : []));
          setProvidersLoaded(true);
        } else if (data.type === 'CONFIG_GET_RESPONSE' || data.type === 'CONFIG_UPDATE_RESPONSE' || data.type === 'CONFIG_RESET_RESPONSE') {
          if (data.payload && data.payload.config) setEngineConfig(data.payload.config);
          if (data.type === 'CONFIG_RESET_RESPONSE' && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            // E6-F04: the provider list carries the defaults the
            // chat model display derives from (is_default /
            // default_model) — refetch it after a reset, like
            // provider save/remove below, or Chat keeps displaying
            // the pre-reset model until a restart.
            wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          }
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
        } else if (data.type === 'ARTIFACT_LIST_RESPONSE') {
          // Epic 8, E8-2.3: ignore global requests meant for GlobalArtifactsView
          if (data.request_id && data.request_id.startsWith('global_artifacts_')) {
            return;
          }
          const list = data.payload?.artifacts || [];
          setCanvasArtifacts(list);
          // D1-A1: the current session comes from the ref — the
          // state variable in this once-registered closure is the
          // mount-time session.
          const currentSessionId = activeSessionRef.current;
          const savedId = currentSessionId ? localStorage.getItem(`aether_canvas_selected_${currentSessionId}`) : null;
          if (savedId && list.some(a => a.id === savedId)) {
            setSelectedArtifactId(savedId);
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({
                type: 'ARTIFACT_GET_REQUEST',
                schema_version: 1,
                request_id: Date.now().toString(),
                payload: { artifact_id: savedId }
              }));
              // D3-19: content for the restored selection is now in
              // flight — the canvas shows its loading state until the
              // ARTIFACT_GET_RESPONSE lands (or the no-answer window
              // App arms alongside expires).
              onArtifactLoadStart(savedId);
            }
          }
        } else if (data.type === 'ARTIFACT_RECORDED') {
          if (data.payload?.session_id === activeSessionRef.current) {
            setCanvasArtifacts(prev => {
              const filtered = prev.filter(a => a.id !== data.payload.artifact_id);
              const artObj = {
                id: data.payload.artifact_id,
                session_id: data.payload.session_id,
                task_id: data.payload.task_id,
                kind: data.payload.kind,
                title: data.payload.title,
                file_path: data.payload.path,
                created_at: data.payload.created_at
              };
              return [artObj, ...filtered];
            });
            // Never auto-opens during task execution per PRD
          }
        } else if (data.type === 'ARTIFACT_GET_RESPONSE') {
          setActiveArtifactContent(data.payload);
          // D3-19: the content this load was waiting for has landed.
          onArtifactLoadEnd();
        } else if (data.type === 'DEV_SERVER_DETECT_RESPONSE') {
          // Epic 8, E8-2.2: dev-server probe result for the Preview tab
          setDevServerInfo(data.payload || null);
        } else if (data.type === 'VOICE_SESSION_START_RESPONSE') {
          // Epic 8, E8-5.1a: ok -> the overlay opens and the mic
          // starts; refused -> the engine's honest reason shows and
          // no device is ever opened.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current) {
            if (p.ok) {
              applyVoiceState({
                ...current,
                status: 'active',
                sessionId: p.session_id || current.sessionId,
                webrtcConfig: p.webrtc_config || null,
                error: null,
                // S15: the degradation ladder's starting
                // profile + the latest start notice (text-only
                // session, energy endpointing, slow-STT
                // warning...), shown in the overlay so a
                // reduced session never looks like a full one.
                profile: p.voice_profile || 'full',
                notice: (Array.isArray(p.notices) && p.notices.length)
                  ? p.notices[p.notices.length - 1]
                  : null,
                // Wave 4: the EFFECTIVE speech profile — the
                // voice speaking, the STT model in effect after
                // any start-time ladder decision, and the
                // configured model it may have stepped down
                // from — for the overlay's profile line.
                ttsVoice: p.tts_voice || null,
                sttModel: p.stt_model || null,
                sttConfiguredModel: p.stt_configured_model || null,
              });
              if (p.session_id && p.session_id !== activeSessionRef.current) {
                setActiveSession(p.session_id);
              }
            } else {
              applyVoiceState({
                ...current,
                status: 'error',
                sessionId: p.session_id || current.sessionId,
                error: p.error || 'Voice is unavailable on this machine.',
                webrtcConfig: null,
              });
            }
          }
        } else if (data.type === 'VOICE_PARTIAL_TRANSCRIPT') {
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            if (p.is_final) {
              // Commit in order: the previous reply if it was still
              // streaming, then this user turn.
              commitVoiceAssistant(current);
              if (p.text) {
                setChatMessages(prev => [...prev, {
                  id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                  role: 'user', content: p.text, isFinal: true,
                }]);
              }
              applyVoiceState({ ...current, partialText: '', assistantText: '', announcementText: '' });
            } else {
              applyVoiceState({ ...current, partialText: p.text || '' });
            }
          }
        } else if (data.type === 'VOICE_OUTPUT_CHUNK') {
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            if (p.text_delta) {
              if (p.announcement_id) {
                // N10: announcement text is not a reply turn. It
                // is tracked apart from assistantText so it can
                // never be committed as a chat message — the
                // announced task's own message already stands in
                // chat — while the overlay still shows what is
                // being announced. Audio below plays as usual.
                applyVoiceState({
                  ...current,
                  announcementText: (current.announcementText || '') + p.text_delta,
                });
              } else {
                applyVoiceState({
                  ...current,
                  assistantText: (current.assistantText || '') + p.text_delta,
                });
              }
            }
            if (p.audio_chunk) {
              voiceChunkSeqRef.current += 1;
              setVoiceAudioChunk({
                seq: voiceChunkSeqRef.current,
                // N09: the engine's chunk identity, when the
                // engine stamps one — the overlay reports finished
                // seqs back as playback progress. Absent (an older
                // engine), the chunk is simply never reported.
                engineSeq: typeof p.chunk_seq === 'number' ? p.chunk_seq : null,
                data: p.audio_chunk,
              });
            }
          }
        } else if (data.type === 'VOICE_BARGE_IN') {
          // E8-5.1b: the client honours the committed-words rule
          // by replacing the streaming bubble with exactly the
          // text that was actually heard, and cuts the overlay's
          // scheduled playback so the unheard tail never plays.
          setVoicePlaybackCut(c => c + 1);
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId) {
            const committed = p.committed_text || '';
            // N10: a barge-in that interrupted an announcement
            // (tagged with its announcement_id) commits nothing —
            // the heard prefix belongs to the task's own message,
            // which already stands in chat.
            if (committed.trim() && !p.announcement_id) {
              setChatMessages(prev => [...prev, {
                id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                role: 'agent', content: committed, thoughts: [], isFinal: true,
              }]);
            }
            applyVoiceState({ ...current, assistantText: '', announcementText: '' });
          }
        } else if (data.type === 'VOICE_SESSION_STATUS') {
          // S15: a mid-session degradation transition. The
          // session only ever steps DOWN the ladder (a slower
          // STT stepped to a faster model, speech output lost
          // -> text-only), announced once with its notice; a
          // profile of "unavailable" means the ladder ended
          // the session — the overlay's error state shows the
          // specific reason, like a refused start.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId) {
            if (p.profile === 'unavailable') {
              applyVoiceState({
                ...current,
                status: 'error',
                profile: 'unavailable',
                error: p.reason || 'Voice became unavailable on this machine.',
                webrtcConfig: null,
              });
            } else {
              applyVoiceState({
                ...current,
                profile: p.profile || current.profile || 'full',
                notice: p.notice || current.notice || null,
                // Wave 4: a step-down names its new model here —
                // the overlay's effective-profile line follows
                // it the moment the transition lands.
                sttModel: p.stt_model || current.sttModel || null,
              });
            }
          }
        } else if (data.type === 'VOICE_SESSION_STOP_RESPONSE') {
          // A local stop already tore the UI down; this only lands
          // for a stop we did not initiate.
          if (voiceStateRef.current) applyVoiceState(null);
        } else if (data.type === 'VOICE_RTC_OFFER_RESPONSE') {
          // F-12b: the engine's answer goes down to the overlay's
          // peer connection; a refusal is the voice error state.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId) {
            if (p.ok && p.answer) {
              voiceRtcSeqRef.current += 1;
              setVoiceRtcAnswer({ seq: voiceRtcSeqRef.current, answer: p.answer });
            } else {
              applyVoiceState({
                ...current,
                status: 'error',
                error: p.error || 'Voice connection failed.',
                webrtcConfig: null,
              });
            }
          }
        } else if (data.type === 'VOICE_RTC_ICE') {
          // F-12b: one of the engine's own ICE candidates (or the
          // null end-of-candidates marker) for the overlay's peer.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            voiceRtcSeqRef.current += 1;
            setVoiceRtcRemoteCandidate({
              seq: voiceRtcSeqRef.current,
              candidate: p.candidate ?? null,
            });
          }
        } else if (data.type === 'VOICE_RTC_ICE_RESPONSE') {
          // S2 (master build): the answer to VOICE_RTC_ICE_REQUEST —
          // the renderer's local candidates going up. ok:true is a
          // pure acknowledgement (the engine's own candidates arrive
          // separately as VOICE_RTC_ICE pushes, handled above), so
          // only a refusal acts: mirror the VOICE_RTC_OFFER_RESPONSE
          // refusal — if the engine says it could not take the
          // candidate for the session the overlay believes is live
          // (e.g. no active voice session), the voice state is the
          // error state, never a live-looking overlay behind which
          // negotiation has actually failed.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId && !p.ok) {
            applyVoiceState({
              ...current,
              status: 'error',
              error: p.error || 'Voice connection failed.',
              webrtcConfig: null,
            });
          }
        } else if (data.type === 'DICTATION_START_RESPONSE') {
          const p = data.payload || {};
          if (p.ok) {
            setDictationState({ status: 'active', error: null, text: '' });
          } else {
            setDictationState({
              status: 'error',
              error: p.error || 'Dictation unavailable',
              text: '',
            });
          }
        } else if (data.type === 'DICTATION_CHUNK_RESPONSE') {
          const p = data.payload || {};
          if (p.text !== undefined && p.text !== null) {
            setDictationState(prev => ({
              ...(prev || {}),
              text: p.text,
            }));
          }
        } else if (data.type === 'DICTATION_STOP_RESPONSE') {
          const p = data.payload || {};
          if (p.text !== undefined && p.text !== null) {
            setDictationState(prev => ({
              ...(prev || {}),
              status: 'idle',
              text: p.text,
            }));
          }
        } else if (data.type === 'READ_ALOUD_START_RESPONSE') {
          const p = data.payload || {};
          if (p.ok) {
            setReadAloudState({
              messageId: p.message_id,
              status: 'playing',
              error: null,
            });
          } else {
            setReadAloudState({
              messageId: p.message_id,
              status: 'error',
              error: p.error || 'TTS unavailable',
            });
          }
        } else if (data.type === 'READ_ALOUD_CHUNK') {
          const p = data.payload || {};
          if (p.audio_chunk) {
            try {
              const Ctx = window.AudioContext || window.webkitAudioContext;
              if (Ctx) {
                if (!readAloudAudioCtxRef.current) readAloudAudioCtxRef.current = new Ctx();
                const ctx = readAloudAudioCtxRef.current;
                const binary = window.atob(p.audio_chunk);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
                const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
                const decoded = ctx.decodeAudioData(buffer);
                if (decoded && typeof decoded.then === 'function') {
                  decoded.then((audioBuffer) => {
                    const source = ctx.createBufferSource();
                    source.buffer = audioBuffer;
                    source.connect(ctx.destination);
                    source.onended = () => { readAloudSourcesRef.current.delete(source); };
                    readAloudSourcesRef.current.add(source);
                    const startAt = Math.max(ctx.currentTime, readAloudPlayHeadRef.current);
                    source.start(startAt);
                    readAloudPlayHeadRef.current = startAt + (audioBuffer.duration || 0);
                  }, () => {
                    // D1-A4: a decode failure was an unhandled
                    // rejection — silent while the button kept
                    // showing "playing". Surface it as this
                    // message's read-aloud error state instead.
                    setReadAloudState(prev => (prev && (!p.message_id || prev.messageId === p.message_id)
                      ? { ...prev, status: 'error', error: 'Read-aloud audio could not be decoded.' }
                      : prev));
                  });
                }
              }
            } catch (err) {
              console.error('Failed to decode read-aloud chunk:', err);
            }
          }
          if (p.is_final) {
            // D1-A4: the engine's final chunk ends the stream — the
            // state returns to idle so the message's button stops
            // presenting Stop/playing for audio that has ended.
            // (Already-scheduled sources still play out; App's stop
            // path remains authoritative for an explicit stop.)
            setReadAloudState(prev => (prev && (!p.message_id || prev.messageId === p.message_id)
              ? { ...prev, status: 'idle' }
              : prev));
          }
        } else if (data.type === 'READ_ALOUD_STOP_RESPONSE') {
          stopAppReadAloud();
          setReadAloudState(null);
        } else if (data.type === 'USAGE_LEDGER_QUERY_RESPONSE') {
          // Epic 8, E8-A8.1a: rebuild the cost surfaces from the
          // ledger rows themselves, so the header total and the
          // popover always reconcile with the recorded calls.
          setCostTasks(groupEntriesByTask(data.payload?.entries || []));
        } else if (data.type === 'TASK_COST_UPDATE') {
          // Epic 8, E8-A8.1a: live tick. The engine read these
          // figures back from the ledger row it just recorded, so
          // merging them keeps the header in step with usage.db.
          const p = data.payload || {};
          if (p.session_id && p.session_id === activeSessionRef.current) {
            setLastCostTaskId(p.task_id);
            setCostTasks(prev => {
              const existing = prev.find(t => t.task_id === p.task_id);
              const base = existing || {
                task_id: p.task_id, cost_usd: 0, input_tokens: 0,
                output_tokens: 0, last_ts: 0, by_model: {},
              };
              const byModel = { ...base.by_model };
              const model = { ...(byModel[p.model] || {
                cost_usd: 0, input_tokens: 0, output_tokens: 0,
                calls: 0, is_local: false, unpriced: false,
              }) };
              model.cost_usd += p.delta_cost_usd || 0;
              model.input_tokens += p.input_tokens || 0;
              model.output_tokens += p.output_tokens || 0;
              model.calls += 1;
              if (p.is_local) model.is_local = true;
              if (p.pricing_status === 'unpriced') model.unpriced = true;
              byModel[p.model] = model;
              const updated = {
                ...base,
                cost_usd: typeof p.task_total_cost_usd === 'number'
                  ? p.task_total_cost_usd
                  : base.cost_usd + (p.delta_cost_usd || 0),
                input_tokens: base.input_tokens + (p.input_tokens || 0),
                output_tokens: base.output_tokens + (p.output_tokens || 0),
                last_ts: Date.now() / 1000,
                by_model: byModel,
              };
              return [updated, ...prev.filter(t => t.task_id !== p.task_id)];
            });
            requestContextBreakdown(p.session_id);
          }
        } else if (data.type === 'CONTEXT_BREAKDOWN_RESPONSE') {
          // Epic 8, E8-A8.1b: segmented context breakdown for header and settings
          if (data.payload) {
            setContextBreakdown(data.payload);
          }
        } else if (data.type === 'DAILY_BUDGET_SET_RESPONSE') {
          // Epic 8, E8-A8.1b: daily budget updated
          setDailyBudget(data.payload?.daily_budget_usd != null ? data.payload.daily_budget_usd : null);
        } else if (data.type === 'DAILY_BUDGET_EXCEEDED') {
          // Epic 8, E8-A8.1b: soft alert on daily budget (running task finishes uninterrupted)
          setBudgetAlert(data.payload);
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

    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
    };
    // The dispatch is registered exactly once, by design (see the
    // C1 note above): the destructured deps are the mount-time
    // bindings — refs, setters and stable callbacks, plus the two
    // first-render closures the original in-App effect also captured
    // (handleSessionSwitch, handleRewindUndoExpire). Re-registering
    // on dep changes would resurrect the duplicate-subscription bug
    // the C1 work removed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
