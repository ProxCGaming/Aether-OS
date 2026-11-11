import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import App from '../App';

// F1 (linkage audit): the dictation audio path, end to end on
// the desktop side. Before the fix the mic stream was opened
// and held unread — no DICTATION_CHUNK_REQUEST existed and the
// STOP carried no audio, so engine STT could never hear a
// word. These tests pin the repaired behaviour:
//   (a) press sends DICTATION_START_REQUEST; the engine's ok
//       starts real capture;
//   (b) captured audio streams DICTATION_CHUNK_REQUESTs with
//       the contract fields (base64 PCM16, sample_rate);
//   (c) release sends DICTATION_STOP_REQUEST carrying the
//       complete utterance and releases the mic (tracks
//       stopped, audio context closed);
//   (d) a refused microphone shows the error state, sends no
//       chunks, and the release still answers START with STOP.

jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeAudioGraph() {
  const processor = { connect: jest.fn(), disconnect: jest.fn(), onaudioprocess: null };
  const source = { connect: jest.fn(), disconnect: jest.fn() };
  const gain = { gain: { value: 1 }, connect: jest.fn(), disconnect: jest.fn() };
  const ctx = {
    currentTime: 0,
    destination: {},
    sampleRate: 48000,
    state: 'running',
    createBufferSource: jest.fn(() => ({ connect: jest.fn(), start: jest.fn(), stop: jest.fn() })),
    createMediaStreamSource: jest.fn(() => source),
    createScriptProcessor: jest.fn(() => processor),
    createGain: jest.fn(() => gain),
    decodeAudioData: jest.fn(() => Promise.resolve({ duration: 2 })),
    resume: jest.fn(() => Promise.resolve()),
    close: jest.fn(() => Promise.resolve()),
  };
  return { ctx, processor, source };
}

function pushAudio(graph, samples) {
  graph.processor.onaudioprocess({
    inputBuffer: { getChannelData: () => Float32Array.from(samples) },
  });
}

function decodePcm(b64) {
  const buf = Buffer.from(b64, 'base64');
  const out = [];
  for (let i = 0; i + 1 < buf.length; i += 2) out.push(buf.readInt16LE(i));
  return out;
}

const flushPromises = async () => {
  await act(async () => {});
};

describe('Dictation audio path (F1)', () => {
  let graph;
  let track;
  let stream;
  let getUserMedia;

  beforeEach(() => {
    graph = makeAudioGraph();
    window.AudioContext = jest.fn(() => graph.ctx);
    window.webkitAudioContext = window.AudioContext;
    track = { stop: jest.fn(), kind: 'audio' };
    stream = { getTracks: jest.fn(() => [track]) };
    getUserMedia = jest.fn(() => Promise.resolve(stream));
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    });
  });

  afterEach(() => {
    delete window.navigator.mediaDevices;
    delete window.AudioContext;
    delete window.webkitAudioContext;
  });

  function renderWindow(handlers, dictationState = null) {
    const element = (state) => (
      <DraggableChatWindow
        messages={[]}
        status="Online"
        isThinking={false}
        providers={[]}
        isFloating={false}
        initialInput=""
        slashCommands={[]}
        isSidebarCollapsed={false}
        onSendMessage={jest.fn()}
        dictationState={state}
        {...handlers}
      />
    );
    const utils = render(element(dictationState));
    return { ...utils, setDictationState: (state) => utils.rerender(element(state)) };
  }

  it('(b)+(c) captured audio streams cumulative chunks; release sends the full utterance on STOP and releases the mic', async () => {
    jest.useFakeTimers();
    try {
      const handlers = {
        onDictationStart: jest.fn(),
        onDictationChunk: jest.fn(),
        onDictationStop: jest.fn(),
      };
      const view = renderWindow(handlers);
      const micBtn = screen.getByTestId('composer-mic-btn');

      fireEvent.mouseDown(micBtn);
      expect(handlers.onDictationStart).toHaveBeenCalledTimes(1);
      expect(getUserMedia).not.toHaveBeenCalled();

      // Engine answers ok -> capture begins.
      view.setDictationState({ status: 'active', error: null, text: '' });
      await flushPromises();
      expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
      expect(micBtn).toHaveAttribute('aria-pressed', 'true');
      expect(graph.ctx.createMediaStreamSource).toHaveBeenCalledWith(stream);

      // 0.1 s of 48 kHz audio -> one chunk after the cadence,
      // resampled to 1600 PCM16 samples at 16000 Hz.
      pushAudio(graph, new Array(4800).fill(0.5));
      act(() => { jest.advanceTimersByTime(2000); });
      expect(handlers.onDictationChunk).toHaveBeenCalledTimes(1);
      const chunk = handlers.onDictationChunk.mock.calls[0][0];
      expect(chunk.sample_rate).toBe(16000);
      expect(decodePcm(chunk.audio).length).toBe(1600);

      // More audio -> the next chunk is cumulative.
      pushAudio(graph, new Array(4800).fill(0.5));
      act(() => { jest.advanceTimersByTime(2000); });
      expect(handlers.onDictationChunk).toHaveBeenCalledTimes(2);
      expect(decodePcm(handlers.onDictationChunk.mock.calls[1][0].audio).length).toBe(3200);

      // Release: STOP carries the complete utterance; mic and
      // graph are released.
      fireEvent.mouseUp(micBtn);
      expect(handlers.onDictationStop).toHaveBeenCalledTimes(1);
      const finalAudio = handlers.onDictationStop.mock.calls[0][0];
      expect(finalAudio.sample_rate).toBe(16000);
      expect(decodePcm(finalAudio.audio).length).toBe(3200);
      expect(track.stop).toHaveBeenCalled();
      expect(graph.ctx.close).toHaveBeenCalled();
      expect(micBtn).toHaveAttribute('aria-pressed', 'false');
    } finally {
      jest.useRealTimers();
    }
  });

  it('(d) a refused microphone shows the error, sends no chunks, and release still answers with STOP', async () => {
    getUserMedia.mockRejectedValue(new Error('Permission denied'));
    const handlers = {
      onDictationStart: jest.fn(),
      onDictationChunk: jest.fn(),
      onDictationStop: jest.fn(),
    };
    const view = renderWindow(handlers);
    const micBtn = screen.getByTestId('composer-mic-btn');

    fireEvent.mouseDown(micBtn);
    expect(handlers.onDictationStart).toHaveBeenCalledTimes(1);

    view.setDictationState({ status: 'active', error: null, text: '' });
    await flushPromises();

    expect(screen.getByTestId('dictation-error')).toHaveTextContent('Microphone unavailable');
    expect(handlers.onDictationChunk).not.toHaveBeenCalled();
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');

    fireEvent.mouseUp(micBtn);
    expect(handlers.onDictationStop).toHaveBeenCalledTimes(1);
  });

  describe('through App (the real send path)', () => {
    let engineListeners;
    let electronAPI;

    beforeEach(() => {
      engineListeners = [];
      electronAPI = {
        sendEngineMessage: jest.fn(),
        onEngineMessage: jest.fn((cb) => {
          engineListeners.push(cb);
          return () => {
            const idx = engineListeners.indexOf(cb);
            if (idx !== -1) engineListeners.splice(idx, 1);
          };
        }),
        getWsStatus: jest.fn(() => Promise.resolve(false)),
        getMemoryUsage: jest.fn(() => Promise.resolve(128)),
        onChatDetached: jest.fn(() => jest.fn()),
        onChatAttached: jest.fn(() => jest.fn()),
      };
      window.electronAPI = electronAPI;
    });

    afterEach(() => {
      delete window.electronAPI;
    });

    const emitEngineMessage = (obj) => {
      const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
      act(() => {
        [...engineListeners].forEach((cb) => cb(raw));
      });
    };

    const sentPayloads = (type) => electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
      .filter((m) => m && m.type === type)
      .map((m) => m.payload);

    it('(a) press sends START, engine ok begins capture, chunks ride the socket, release sends STOP with the final audio', async () => {
      render(<App />);
      await flushPromises();
      emitEngineMessage({ type: '_ws_status', status: 'connected' });
      await flushPromises();
      emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
      await flushPromises();

      const micBtn = screen.getByTestId('composer-mic-btn');
      fireEvent.mouseDown(micBtn);
      await flushPromises();
      expect(sentPayloads('DICTATION_START_REQUEST')).toEqual([{ session_id: 's1' }]);
      expect(getUserMedia).not.toHaveBeenCalled();

      emitEngineMessage({
        type: 'DICTATION_START_RESPONSE',
        payload: { ok: true, error: null },
      });
      await flushPromises();
      expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
      expect(micBtn).toHaveAttribute('aria-pressed', 'true');

      // Speak, then wait one real chunk cadence.
      pushAudio(graph, new Array(4800).fill(0.5));
      await act(async () => {
        await new Promise((resolve) => { setTimeout(resolve, 2300); });
      });
      const chunks = sentPayloads('DICTATION_CHUNK_REQUEST');
      expect(chunks.length).toBe(1);
      expect(chunks[0].session_id).toBe('s1');
      expect(chunks[0].sample_rate).toBe(16000);
      expect(chunks[0].is_final).toBe(false);
      expect(decodePcm(chunks[0].audio).length).toBe(1600);

      fireEvent.mouseUp(micBtn);
      await flushPromises();
      const stops = sentPayloads('DICTATION_STOP_REQUEST');
      expect(stops.length).toBe(1);
      expect(stops[0].session_id).toBe('s1');
      expect(stops[0].sample_rate).toBe(16000);
      expect(decodePcm(stops[0].audio).length).toBe(1600);
      expect(track.stop).toHaveBeenCalled();
      expect(graph.ctx.close).toHaveBeenCalled();
    }, 20000);
  });
});
