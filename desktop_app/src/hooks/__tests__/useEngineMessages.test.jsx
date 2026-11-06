import React from 'react';
import { render, act } from '@testing-library/react';
import { useEngineMessages } from '../useEngineMessages';

// Wave 5, Annex C row 5 (message-router seam): contract tests for the
// extracted engine-message dispatch. The hook is driven through a
// fake socket facade — the same surface App hands it — and the deps
// are the refs / setters / callbacks it dispatches to. These tests
// pin the registration discipline (exactly one subscription, removed
// on unmount) and a representative slice of the dispatch branches,
// including the two D3-19 load-lifecycle reports.

global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeDeps() {
  const added = [];
  const removed = [];
  const ws = {
    readyState: 1,
    send: jest.fn(),
    addEventListener: jest.fn((type, fn) => { if (type === 'message') added.push(fn); }),
    removeEventListener: jest.fn((type, fn) => { if (type === 'message') removed.push(fn); }),
  };
  const setter = () => jest.fn();
  const deps = {
    wsRef: { current: ws },
    isFloating: false,
    initialSession: null,
    activeSessionRef: { current: 's1' },
    activeTaskSessionRef: { current: null },
    browserSessionIdsRef: { current: new Set() },
    chatMessagesRef: { current: [] },
    isThinkingRef: { current: false },
    moveBatchRef: { current: null },
    pendingApprovalsRef: { current: [] },
    pendingSessionSwitchIdRef: { current: null },
    pendingSessionSwitchRef: { current: null },
    pendingThinkingRef: { current: {} },
    readAloudAudioCtxRef: { current: null },
    readAloudPlayHeadRef: { current: 0 },
    readAloudSourcesRef: { current: [] },
    rewindUndoTimerRef: { current: null },
    sessionCacheRef: { current: {} },
    sessionSearchQueryRef: { current: '' },
    sessionsRef: { current: [] },
    voiceChunkSeqRef: { current: 0 },
    voiceRtcSeqRef: { current: 0 },
    voiceStateRef: { current: null },
    setActiveArtifactContent: setter(),
    setActiveSession: setter(),
    setActiveTaskIds: setter(),
    setBrowserSessions: setter(),
    setBudgetAlert: setter(),
    setCanvasArtifacts: setter(),
    setChatMessages: setter(),
    setContextBreakdown: setter(),
    setCostTasks: setter(),
    setDailyBudget: setter(),
    setDevServerInfo: setter(),
    setDictationState: setter(),
    setEngineConfig: setter(),
    setExportStatus: setter(),
    setImportStatus: setter(),
    setIsThinking: setter(),
    setLastCostTaskId: setter(),
    setLocalModels: setter(),
    setMoveStatus: setter(),
    setPlanModeBySession: setter(),
    setPluginCount: setter(),
    setProviderHealth: setter(),
    setProviderHealthLoaded: setter(),
    setProviders: setter(),
    setProvidersLoaded: setter(),
    setReadAloudState: setter(),
    setRewindConfirm: setter(),
    setRewindManifest: setter(),
    setRewindUndo: setter(),
    setScratchpadBySession: setter(),
    setSelectedArtifactId: setter(),
    setSelectedSessionIds: setter(),
    setSessionSearchResults: setter(),
    setSessionSelectMode: setter(),
    setSessionSwitchPendingId: setter(),
    setSessions: setter(),
    setSlashCommands: setter(),
    setStatus: setter(),
    setVoiceAudioChunk: setter(),
    setVoicePlaybackCut: setter(),
    setVoiceRtcAnswer: setter(),
    setVoiceRtcRemoteCandidate: setter(),
    setWorkspaces: setter(),
    applyVoiceState: jest.fn(),
    clearRewindUndo: jest.fn(),
    commitVoiceAssistant: jest.fn(),
    handleRewindUndoExpire: jest.fn(),
    handleSessionSwitch: jest.fn(),
    requestContextBreakdown: jest.fn(),
    requestCostBreakdown: jest.fn(),
    requestRewindManifest: jest.fn(),
    requestWorkspaceList: jest.fn(),
    sendSessionListRequest: jest.fn(),
    stopAppReadAloud: jest.fn(),
    onArtifactLoadStart: jest.fn(),
    onArtifactLoadEnd: jest.fn(),
  };
  return { deps, ws, added, removed };
}

function Probe({ deps }) {
  useEngineMessages(deps);
  return null;
}

function mount() {
  const ctx = makeDeps();
  const utils = render(<Probe deps={ctx.deps} />);
  const emit = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => { ctx.added.forEach((fn) => fn({ data: raw })); });
  };
  return { ...ctx, ...utils, emit };
}

describe('useEngineMessages', () => {
  afterEach(() => {
    localStorage.clear();
    jest.restoreAllMocks();
  });

  it('registers exactly one message subscription and removes it on unmount', () => {
    const { deps, ws, added, removed, unmount } = mount();
    expect(ws.addEventListener).toHaveBeenCalledTimes(1);
    expect(added).toHaveLength(1);
    unmount();
    expect(ws.removeEventListener).toHaveBeenCalledTimes(1);
    expect(removed).toEqual(added);
    expect(deps).toBeDefined();
  });

  it('hello reports the online status with the active model', () => {
    const { deps, emit } = mount();
    emit({ type: 'hello', payload: { active_model: 'gemini-test' } });
    expect(deps.setStatus).toHaveBeenCalledWith('Online / gemini-test');
  });

  it('survives a payload-less hello (D1-A9) and a malformed frame', () => {
    const { deps, emit } = mount();
    expect(() => emit({ type: 'hello' })).not.toThrow();
    expect(() => emit('this is not json')).not.toThrow();
    expect(deps.setChatMessages).not.toHaveBeenCalled();
  });

  it('START_TASK marks thinking, tracks the task session and asks for the rewind manifest', () => {
    const { deps, emit } = mount();
    emit({ type: 'START_TASK', request_id: 'r1', payload: { task_id: 't1', session_id: 's1' } });
    expect(deps.setIsThinking).toHaveBeenCalledWith(true);
    expect(deps.activeTaskSessionRef.current).toBe('s1');
    expect(deps.requestRewindManifest).toHaveBeenCalledWith('s1');
    expect(deps.setChatMessages).toHaveBeenCalledWith(expect.any(Function));
  });

  it('ARTIFACT_GET_RESPONSE stores the content and ends the D3-19 load', () => {
    const { deps, emit } = mount();
    const payload = { artifact_id: 'art-1', content: 'hello', kind: 'text' };
    emit({ type: 'ARTIFACT_GET_RESPONSE', payload });
    expect(deps.setActiveArtifactContent).toHaveBeenCalledWith(payload);
    expect(deps.onArtifactLoadEnd).toHaveBeenCalledTimes(1);
  });

  it('ARTIFACT_LIST_RESPONSE restores the saved selection, requests its content and starts the D3-19 load', () => {
    const { deps, ws, emit } = mount();
    localStorage.setItem('aether_canvas_selected_s1', 'art-9');
    emit({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'list-1',
      payload: { artifacts: [{ id: 'art-9', title: 'kept.txt' }] },
    });
    expect(deps.setSelectedArtifactId).toHaveBeenCalledWith('art-9');
    const sent = ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
    expect(sent.some((p) => p.type === 'ARTIFACT_GET_REQUEST' && p.payload.artifact_id === 'art-9')).toBe(true);
    expect(deps.onArtifactLoadStart).toHaveBeenCalledWith('art-9');
  });

  it('ARTIFACT_LIST_RESPONSE for the global view is ignored (E8-2.3)', () => {
    const { deps, emit } = mount();
    emit({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'global_artifacts_42',
      payload: { artifacts: [{ id: 'art-1' }] },
    });
    expect(deps.setCanvasArtifacts).not.toHaveBeenCalled();
    expect(deps.onArtifactLoadStart).not.toHaveBeenCalled();
  });
});
