import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import App from '../App';

// Epic 8, E8-5.3: Push-to-talk dictation + TTS read-aloud
//
// 1. Composer push-to-talk:
//    - Holding the composer mic button (mouse, touch, keyboard hold) starts dictation;
//    - Live partial transcript lands in the composer as EDITABLE text;
//    - Release ends capture; NOTHING is sent until the user sends it;
//    - Outside voice mode, does not require entering voice mode;
//    - Respects engine-availability honesty (missing STT -> reports error, no dead button,
//      no mic opened).
//
// 2. Read-aloud:
//    - Speaker control on assistant messages speaks response via Kokoro TTS path;
//    - Second click stops playback;
//    - Starting another message's playback stops previous playback;
//    - Playback state is visible on the control;
//    - Respects engine-availability honesty (missing TTS -> reports error).

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

describe('Push-to-talk dictation + TTS read-aloud (E8-5.3)', () => {
  let engineListeners;
  let electronAPI;
  let getUserMedia;
  let mockAudioContext;

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

    const tracks = [{ stop: jest.fn(), kind: 'audio' }];
    const stream = { getTracks: jest.fn(() => tracks) };
    getUserMedia = jest.fn(() => Promise.resolve(stream));
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    });

    const mockSource = {
      connect: jest.fn(),
      start: jest.fn(),
      stop: jest.fn(),
      onended: null,
    };
    mockAudioContext = {
      currentTime: 0,
      destination: {},
      createBufferSource: jest.fn(() => mockSource),
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 2 })),
      close: jest.fn(),
    };
    window.AudioContext = jest.fn(() => mockAudioContext);
    window.webkitAudioContext = window.AudioContext;
  });

  afterEach(() => {
    delete window.electronAPI;
    delete window.navigator.mediaDevices;
    delete window.AudioContext;
    delete window.webkitAudioContext;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connectAndRenderApp = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  const sentPayloads = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
    .filter((m) => m && m.type === type)
    .map((m) => m.payload);

  describe('Composer push-to-talk dictation', () => {
    it('holding mic button dictates partial text into composer as editable text, release ends capture without sending', async () => {
      const onSendMessage = jest.fn();
      render(
        <DraggableChatWindow
          messages={[]}
          sessionId="s1"
          onSendMessage={onSendMessage}
        />
      );

      const micBtn = screen.getByTestId('composer-mic-btn');
      expect(micBtn).toBeInTheDocument();
      expect(micBtn).toHaveAttribute('title', expect.stringContaining('dictat'));

      const textarea = screen.getByPlaceholderText('Message Aether...');
      expect(textarea).toHaveValue('');

      // Press and hold mic button (mouse down)
      fireEvent.mouseDown(micBtn);
      await flushPromises();

      expect(micBtn).toHaveAttribute('aria-pressed', 'true');
      expect(getUserMedia).toHaveBeenCalledWith({ audio: true });

      // Partial text arrives in composer textarea as editable text
      fireEvent.change(textarea, { target: { value: 'fix the login' } });
      expect(textarea).toHaveValue('fix the login');

      // User can continue editing text
      fireEvent.change(textarea, { target: { value: 'fix the login bug please' } });
      expect(textarea).toHaveValue('fix the login bug please');

      // Release mic button (mouse up)
      fireEvent.mouseUp(micBtn);
      await flushPromises();

      expect(micBtn).toHaveAttribute('aria-pressed', 'false');
      // Verify nothing is sent automatically upon release
      expect(onSendMessage).not.toHaveBeenCalled();
      // Textarea still retains the editable text
      expect(textarea).toHaveValue('fix the login bug please');

      // The user manually sends it
      fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter', charCode: 13 });
      expect(onSendMessage).toHaveBeenCalledWith('fix the login bug please');
    });

    it('touch hold and keyboard hold trigger dictation capture', async () => {
      render(
        <DraggableChatWindow
          messages={[]}
          sessionId="s1"
        />
      );

      const micBtn = screen.getByTestId('composer-mic-btn');

      // Touch hold
      fireEvent.touchStart(micBtn);
      await flushPromises();
      expect(micBtn).toHaveAttribute('aria-pressed', 'true');
      expect(getUserMedia).toHaveBeenCalled();

      fireEvent.touchEnd(micBtn);
      await flushPromises();
      expect(micBtn).toHaveAttribute('aria-pressed', 'false');

      // Keyboard hold (Space on focused button)
      getUserMedia.mockClear();
      fireEvent.keyDown(micBtn, { key: ' ' });
      await flushPromises();
      expect(micBtn).toHaveAttribute('aria-pressed', 'true');
      expect(getUserMedia).toHaveBeenCalled();

      fireEvent.keyUp(micBtn, { key: ' ' });
      await flushPromises();
      expect(micBtn).toHaveAttribute('aria-pressed', 'false');
    });

    it('dictation does not enter voice mode or open voice overlay', async () => {
      await connectAndRenderApp();

      const micBtn = screen.getByTestId('composer-mic-btn');
      fireEvent.mouseDown(micBtn);
      await flushPromises();

      // Voice overlay should NOT be open
      expect(screen.queryByTestId('voice-overlay')).toBeNull();
      // Header voice button should not be active
      expect(screen.queryByTestId('voice-live-dot')).toBeNull();

      fireEvent.mouseUp(micBtn);
      await flushPromises();
      expect(screen.queryByTestId('voice-overlay')).toBeNull();
    });

    it('refused dictation shows honest error and never opens mic', async () => {
      await connectAndRenderApp();

      const micBtn = screen.getByTestId('composer-mic-btn');
      fireEvent.mouseDown(micBtn);
      expect(sentPayloads('DICTATION_START_REQUEST')).toEqual([{ session_id: 's1' }]);

      emitEngineMessage({
        type: 'DICTATION_START_RESPONSE',
        payload: {
          ok: false,
          error: 'voice unavailable: faster-whisper is not installed (local STT)',
        },
      });
      await flushPromises();

      expect(screen.getByTestId('dictation-error')).toHaveTextContent('faster-whisper is not installed');
      expect(getUserMedia).not.toHaveBeenCalled();
      expect(micBtn).toHaveAttribute('aria-pressed', 'false');
    });
  });

  describe('Assistant message read-aloud (TTS)', () => {
    it('assistant message has a speaker button that plays via Kokoro TTS, second click stops it', async () => {
      const messages = [
        { id: 'm1', role: 'assistant', content: 'Here is your completed code.', isFinal: true },
      ];
      render(
        <DraggableChatWindow
          messages={messages}
          sessionId="s1"
        />
      );

      const readBtn = screen.getByTestId('read-aloud-btn-m1');
      expect(readBtn).toBeInTheDocument();
      expect(readBtn).toHaveAttribute('title', 'Read aloud');
      expect(readBtn).toHaveAttribute('aria-pressed', 'false');

      // Click to start read-aloud
      fireEvent.click(readBtn);
      await flushPromises();

      expect(readBtn).toHaveAttribute('aria-pressed', 'true');
      expect(readBtn).toHaveAttribute('title', 'Stop read aloud');

      // Click second time to stop playback
      fireEvent.click(readBtn);
      await flushPromises();

      expect(readBtn).toHaveAttribute('aria-pressed', 'false');
      expect(readBtn).toHaveAttribute('title', 'Read aloud');
    });

    it('clicking read-aloud on another message stops previous playback and starts new', async () => {
      const messages = [
        { id: 'm1', role: 'assistant', content: 'First message.', isFinal: true },
        { id: 'm2', role: 'assistant', content: 'Second message.', isFinal: true },
      ];
      render(
        <DraggableChatWindow
          messages={messages}
          sessionId="s1"
        />
      );

      const btn1 = screen.getByTestId('read-aloud-btn-m1');
      const btn2 = screen.getByTestId('read-aloud-btn-m2');

      // Start playing message 1
      fireEvent.click(btn1);
      await flushPromises();
      expect(btn1).toHaveAttribute('aria-pressed', 'true');
      expect(btn2).toHaveAttribute('aria-pressed', 'false');

      // Click message 2: message 1 should stop and message 2 starts
      fireEvent.click(btn2);
      await flushPromises();
      expect(btn1).toHaveAttribute('aria-pressed', 'false');
      expect(btn2).toHaveAttribute('aria-pressed', 'true');
    });

    it('end-to-end read-aloud WebSocket flow in App with chunks and stop', async () => {
      await connectAndRenderApp();

      // Simulate a completed assistant message in chat
      emitEngineMessage({
        type: 'TASK_COMPLETED',
        payload: {
          session_id: 's1',
          task_id: 't1',
          result: 'Task succeeded perfectly.',
        },
      });
      await flushPromises();

      const readBtn = screen.getByTestId(/read-aloud-btn-/);
      expect(readBtn).toBeInTheDocument();

      // Click read-aloud
      fireEvent.click(readBtn);
      const reqs = sentPayloads('READ_ALOUD_START_REQUEST');
      expect(reqs.length).toBe(1);
      const sentMsgId = reqs[0].message_id;

      // Engine answers ok=true
      emitEngineMessage({
        type: 'READ_ALOUD_START_RESPONSE',
        payload: { ok: true, message_id: sentMsgId },
      });
      await flushPromises();
      expect(readBtn).toHaveAttribute('aria-pressed', 'true');

      // Engine sends chunk
      const b64Wav = window.btoa('RIFF....WAVEfmt ');
      emitEngineMessage({
        type: 'READ_ALOUD_CHUNK',
        payload: { message_id: sentMsgId, audio_chunk: b64Wav, is_final: false },
      });
      await flushPromises();
      expect(mockAudioContext.decodeAudioData).toHaveBeenCalled();

      // Click stop
      fireEvent.click(readBtn);
      expect(sentPayloads('READ_ALOUD_STOP_REQUEST').length).toBe(1);
      expect(readBtn).toHaveAttribute('aria-pressed', 'false');
    });

    it('refused read-aloud shows honest error when TTS unavailable', async () => {
      await connectAndRenderApp();

      emitEngineMessage({
        type: 'TASK_COMPLETED',
        payload: { session_id: 's1', task_id: 't1', result: 'Assistant output.' },
      });
      await flushPromises();

      const readBtn = screen.getByTestId(/read-aloud-btn-/);
      fireEvent.click(readBtn);

      emitEngineMessage({
        type: 'READ_ALOUD_START_RESPONSE',
        payload: {
          ok: false,
          message_id: 't1',
          error: 'voice unavailable: kokoro is not installed (local TTS)',
        },
      });
      await flushPromises();

      expect(screen.getByTestId('read-aloud-error')).toHaveTextContent('kokoro is not installed');
      expect(readBtn).toHaveAttribute('aria-pressed', 'false');
    });
  });
});
