import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VoiceOverlay from '../VoiceOverlay';
import SnipOverlay from '../SnipOverlay';
import CommandPalette from '../CommandPalette';
import Onboarding from '../Onboarding';
import ProjectPreview from '../renderers/ProjectPreview';
import { useAetherSocket } from '../../contexts/SocketContext';

jest.mock('../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Wave 3 (D3 chunk) — overlays, palette, onboarding, preview:
//   D3-05 — VoiceOverlay: per-session RTC sequence refs reset on
//           teardown, or session 2 drops session 1's seq numbers.
//   D3-22 — SnipOverlay: a drag released outside the window must
//           end the selection, not strand it.
//   D3-23 — CommandPalette: selection clamps when items shrink.
//   D3-13 — Onboarding: a rejected folder dialog is surfaced.
//   D3-10 — Onboarding: hand-minted request ids are unique too.
//   D3-27 — ProjectPreview: Go on a static artifact's file path
//           must not turn it into a dead http:// URL.

const flush = async () => { await act(async () => {}); };

describe('D3-05 — VoiceOverlay sequence refs reset between sessions', () => {
  let pc;

  const makePc = () => {
    const instance = {
      addTrack: jest.fn(),
      close: jest.fn(),
      connectionState: 'new',
      signalingState: 'stable',
      localDescription: { type: 'offer', sdp: 'local-offer-sdp' },
      createOffer: jest.fn(() => Promise.resolve({ type: 'offer', sdp: 'raw-offer' })),
      setLocalDescription: jest.fn(() => Promise.resolve()),
      setRemoteDescription: jest.fn(() => Promise.resolve()),
      addIceCandidate: jest.fn(() => Promise.resolve()),
      onicecandidate: null,
      onconnectionstatechange: null,
    };
    return instance;
  };

  const baseProps = {
    open: true, active: true, status: 'listening', partialText: '',
    assistantText: '', error: null, webrtcConfig: { iceServers: [] },
    audioChunk: null, playbackCut: 0,
    onStop: jest.fn(), onDismiss: jest.fn(), onRtcOffer: jest.fn(),
    rtcAnswer: null, onRtcIceCandidate: jest.fn(), rtcRemoteCandidate: null,
  };

  beforeEach(() => {
    const stream = { getTracks: jest.fn(() => [{ stop: jest.fn(), kind: 'audio' }]) };
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: jest.fn(() => Promise.resolve(stream)) },
    });
    pc = makePc();
    window.RTCPeerConnection = jest.fn(() => pc);
  });

  afterEach(() => {
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
  });

  it('applies an answer/candidate with the same seq in the NEXT session', async () => {
    const candidate = { candidate: 'candidate:1', sdpMid: '0', sdpMLineIndex: 0 };
    const answer = { type: 'answer', sdp: 'engine-answer-sdp' };

    // Session 1: candidate seq 3 queued, answer seq 3 applies both.
    const first = render(<VoiceOverlay {...baseProps} />);
    await flush();
    first.rerender(<VoiceOverlay {...baseProps} rtcRemoteCandidate={{ seq: 3, candidate }} />);
    await flush();
    first.rerender(<VoiceOverlay
      {...baseProps}
      rtcRemoteCandidate={{ seq: 3, candidate }}
      rtcAnswer={{ seq: 3, answer }}
    />);
    await flush();
    expect(pc.setRemoteDescription).toHaveBeenCalledTimes(1);
    expect(pc.addIceCandidate).toHaveBeenCalledTimes(1);

    // Session ends (overlay closes -> capture teardown).
    first.rerender(<VoiceOverlay {...baseProps} open={false} active={false} />);
    await flush();

    // Session 2: a fresh peer connection; the engine restarts its
    // sequence numbers, so seq 3 arrives again — it is NOT a
    // duplicate of session 1's and must be applied.
    const pc2 = makePc();
    window.RTCPeerConnection = jest.fn(() => pc2);
    first.rerender(<VoiceOverlay {...baseProps} />);
    await flush();
    first.rerender(<VoiceOverlay {...baseProps} rtcRemoteCandidate={{ seq: 3, candidate }} />);
    await flush();
    first.rerender(<VoiceOverlay
      {...baseProps}
      rtcRemoteCandidate={{ seq: 3, candidate }}
      rtcAnswer={{ seq: 3, answer }}
    />);
    await flush();
    expect(pc2.setRemoteDescription).toHaveBeenCalledTimes(1);
    expect(pc2.addIceCandidate).toHaveBeenCalledWith(candidate);
  });
});

describe('D3-22 — SnipOverlay selection ends wherever the mouse is released', () => {
  const onCapture = jest.fn();
  const onCancel = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
    window.electronAPI = { captureScreen: jest.fn(async () => 'data:image/png;base64,AAAA') };
  });

  afterEach(() => { delete window.electronAPI; });

  it('a mouseup on the window (outside the overlay surface) completes the capture', async () => {
    render(<SnipOverlay isOpen onCapture={onCapture} onCancel={onCancel} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { clientX: 10, clientY: 10, button: 0 });
    fireEvent.mouseMove(overlay, { clientX: 110, clientY: 60 });
    await act(async () => { fireEvent.mouseUp(window, { clientX: 110, clientY: 60 }); });
    expect(onCapture).toHaveBeenCalledTimes(1);
    expect(onCapture.mock.calls[0][0].bounds).toEqual({ x: 10, y: 10, width: 100, height: 50 });
    expect(screen.queryByTestId('snip-selection-box')).not.toBeInTheDocument();
  });

  it('losing window focus mid-drag cancels the selection instead of stranding it', async () => {
    render(<SnipOverlay isOpen onCapture={onCapture} onCancel={onCancel} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { clientX: 10, clientY: 10, button: 0 });
    fireEvent.mouseMove(overlay, { clientX: 80, clientY: 40 });
    expect(screen.getByTestId('snip-selection-box')).toBeInTheDocument();
    fireEvent.blur(window);
    expect(screen.queryByTestId('snip-selection-box')).not.toBeInTheDocument();
    await act(async () => { fireEvent.mouseUp(window, { clientX: 80, clientY: 40 }); });
    expect(onCapture).not.toHaveBeenCalled();
  });
});

describe('D3-23 — CommandPalette selection clamps when items shrink', () => {
  it('Enter runs the clamped selection after the items prop shrinks', () => {
    const runs = [];
    const many = Array.from({ length: 12 }, (_, i) => ({
      id: `item-${i}`, kind: 'action', label: `Action ${i}`,
      run: jest.fn(() => runs.push(i)),
    }));
    const onClose = jest.fn();
    const { rerender } = render(<CommandPalette open items={many} onClose={onClose} />);
    const input = screen.getByPlaceholderText(/Type a command/);
    for (let i = 0; i < 5; i += 1) fireEvent.keyDown(input, { key: 'ArrowDown' });

    const two = many.slice(0, 2);
    rerender(<CommandPalette open items={two} onClose={onClose} />);
    fireEvent.keyDown(input, { key: 'Enter' });
    // Selection was 5; with two results it must clamp to index 1 —
    // pre-fix Enter addressed results[5] (undefined) and did nothing.
    expect(two[1].run).toHaveBeenCalledTimes(1);
    expect(two[0].run).not.toHaveBeenCalled();
  });
});

describe('D3-13 / D3-10 — Onboarding dialog failures and request ids', () => {
  let mockSendMessage;
  let listeners;

  const providers = [{ name: 'google_gemini', display_name: 'Google Gemini', has_key: false }];
  const emit = (data) => act(() => listeners.forEach((cb) => cb({ data: JSON.stringify(data) })));

  beforeEach(() => {
    mockSendMessage = jest.fn();
    listeners = [];
    useAetherSocket.mockImplementation(() => ({
      sendMessage: mockSendMessage,
      isConnected: true,
      wsRef: {
        current: {
          addEventListener: jest.fn((ev, cb) => { if (ev === 'message') listeners.push(cb); }),
          removeEventListener: jest.fn(),
          readyState: 1,
        },
      },
    }));
  });

  afterEach(() => { delete window.electronAPI; jest.restoreAllMocks(); });

  it('surfaces a rejected folder dialog as a visible error', async () => {
    window.electronAPI = {
      showOpenDialog: jest.fn(async () => { throw new Error('ipc blew up'); }),
    };
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByText('Choose folder…')); });
    expect(screen.getByText(/could not be added/i)).toBeInTheDocument();
  });

  it('mints a distinct request_id for each of the three step saves', async () => {
    jest.spyOn(Date, 'now').mockReturnValue(1700000000000);
    window.electronAPI = { showOpenDialog: jest.fn(async () => '/data/ws') };
    render(<Onboarding providers={providers} onDone={jest.fn()} />);

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();

    await act(async () => { fireEvent.click(screen.getByText('Choose folder…')); });
    emit({ type: 'WORKSPACE_ADD_RESPONSE', payload: { success: true, workspace: { path: '/data/ws' } } });
    expect(screen.getByText('Approval default')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Continue'));

    const ids = mockSendMessage.mock.calls.map(([, payload]) => payload.request_id);
    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
  });
});

describe('D3-27 — ProjectPreview Go in static mode', () => {
  const htmlArtifact = {
    id: 'art-html', kind: 'html', title: 'index.html',
    file_path: '/site/index.html', created_at: 1000,
  };
  const noServer = { running: false, port: null, url: null, framework: null };

  it('pressing Go with the artifact path in the bar keeps the static file preview', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={'<html><body>hi</body></html>'}
        devServer={noServer}
        onDetect={jest.fn()}
      />
    );
    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.getAttribute('src')).toBe('file:///site/index.html');
    const input = screen.getByTestId('preview-url-input');
    expect(input).toHaveValue('/site/index.html');
    fireEvent.submit(input.closest('form'));
    expect(screen.getByTestId('project-preview-iframe').getAttribute('src'))
      .toBe('file:///site/index.html');
  });

  it('typing a real server address still switches to server mode', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={'<html><body>hi</body></html>'}
        devServer={noServer}
        onDetect={jest.fn()}
      />
    );
    const input = screen.getByTestId('preview-url-input');
    fireEvent.change(input, { target: { value: 'localhost:3000' } });
    fireEvent.submit(input.closest('form'));
    expect(screen.getByTestId('project-preview-iframe').getAttribute('src'))
      .toBe('http://localhost:3000');
  });
});
