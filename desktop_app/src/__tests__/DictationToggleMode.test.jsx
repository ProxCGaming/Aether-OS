import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

// Wave 4: dictation interaction mode is a SETTING with both
// behaviours — hold-to-talk (the historical default) and
// click-toggle — selectable in Settings → Voice. Both run
// through the ONE capture path (dictationCapture.js) and the
// selected microphone (inputDeviceId). These tests pin the
// toggle behaviour and the device constraint on both capture
// routes (the direct getUserMedia route and the
// engine-answered route).

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

describe('Dictation interaction modes (Wave 4)', () => {
  let getUserMedia;
  let tracks;
  let stream;

  beforeEach(() => {
    tracks = [{ stop: jest.fn(), kind: 'audio' }];
    stream = { getTracks: jest.fn(() => tracks) };
    getUserMedia = jest.fn(() => Promise.resolve(stream));
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    });
    const mockSource = {
      connect: jest.fn(), start: jest.fn(), stop: jest.fn(),
      disconnect: jest.fn(), onended: null,
    };
    const mockProcessor = { connect: jest.fn(), disconnect: jest.fn(), onaudioprocess: null };
    const mockGain = { gain: { value: 1 }, connect: jest.fn(), disconnect: jest.fn() };
    window.AudioContext = jest.fn(() => ({
      currentTime: 0,
      destination: {},
      sampleRate: 48000,
      state: 'running',
      createBufferSource: jest.fn(() => mockSource),
      createMediaStreamSource: jest.fn(() => mockSource),
      createScriptProcessor: jest.fn(() => mockProcessor),
      createGain: jest.fn(() => mockGain),
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 2 })),
      close: jest.fn(),
    }));
    window.webkitAudioContext = window.AudioContext;
  });

  afterEach(() => {
    delete window.navigator.mediaDevices;
    delete window.AudioContext;
    delete window.webkitAudioContext;
  });

  const flushPromises = async () => { await act(async () => {}); };

  it('toggle mode: one click starts dictation, press/release do not stop it, a second click stops', async () => {
    render(<DraggableChatWindow messages={[]} sessionId="s1" dictationMode="toggle" />);
    const micBtn = screen.getByTestId('composer-mic-btn');
    expect(micBtn).toHaveAttribute('title', 'Click to dictate');

    fireEvent.click(micBtn);
    await flushPromises();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    expect(micBtn).toHaveAttribute('title', 'Click to finish dictation');

    // A press/release cycle (which precedes every real click)
    // must not end a toggle dictation.
    fireEvent.mouseDown(micBtn);
    fireEvent.mouseUp(micBtn);
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    expect(tracks[0].stop).not.toHaveBeenCalled();

    fireEvent.click(micBtn);
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it('toggle mode captures from the selected microphone', async () => {
    render(
      <DraggableChatWindow
        messages={[]} sessionId="s1"
        dictationMode="toggle" inputDeviceId="mic-42"
      />,
    );
    fireEvent.click(screen.getByTestId('composer-mic-btn'));
    await flushPromises();
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { deviceId: { exact: 'mic-42' } },
    });
  });

  it('toggle mode: Space keydown toggles, keyup does not end it, key repeat does not re-toggle', async () => {
    render(<DraggableChatWindow messages={[]} sessionId="s1" dictationMode="toggle" />);
    const micBtn = screen.getByTestId('composer-mic-btn');
    fireEvent.keyDown(micBtn, { key: ' ' });
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    fireEvent.keyUp(micBtn, { key: ' ' });
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    fireEvent.keyDown(micBtn, { key: ' ', repeat: true });
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    fireEvent.keyDown(micBtn, { key: ' ' });
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');
  });

  it('toggle mode through the engine path: click starts, the engine-answered capture uses the selected mic, click stops', async () => {
    const onDictationStart = jest.fn();
    const onDictationStop = jest.fn();
    const { rerender } = render(
      <DraggableChatWindow
        messages={[]} sessionId="s1"
        dictationMode="toggle" inputDeviceId="mic-7"
        dictationState={{ status: 'idle', text: null, error: null }}
        onDictationStart={onDictationStart}
        onDictationStop={onDictationStop}
        onDictationChunk={jest.fn()}
      />,
    );
    const micBtn = screen.getByTestId('composer-mic-btn');
    fireEvent.click(micBtn);
    await flushPromises();
    expect(onDictationStart).toHaveBeenCalledTimes(1);
    // Capture waits for the engine's answer, as in hold mode.
    expect(getUserMedia).not.toHaveBeenCalled();

    rerender(
      <DraggableChatWindow
        messages={[]} sessionId="s1"
        dictationMode="toggle" inputDeviceId="mic-7"
        dictationState={{ status: 'active', text: null, error: null }}
        onDictationStart={onDictationStart}
        onDictationStop={onDictationStop}
        onDictationChunk={jest.fn()}
      />,
    );
    await flushPromises();
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { deviceId: { exact: 'mic-7' } },
    });
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(micBtn);
    await flushPromises();
    expect(onDictationStop).toHaveBeenCalledTimes(1);
  });

  it('hold mode (the default) ignores clicks: only press-and-hold dictates', async () => {
    render(<DraggableChatWindow messages={[]} sessionId="s1" />);
    const micBtn = screen.getByTestId('composer-mic-btn');
    expect(micBtn).toHaveAttribute('title', 'Hold to dictate');
    fireEvent.click(micBtn);
    await flushPromises();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');
    fireEvent.mouseDown(micBtn);
    await flushPromises();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    fireEvent.mouseUp(micBtn);
    await flushPromises();
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');
  });
});
