import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import fs from 'fs';
import path from 'path';
import App from '../App';

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

// S3-M7: the floating-window transfer state is the FULL chat history.
// It used to be persisted in plaintext localStorage under
// 'floating_transfer_state', readable by any script in the renderer.
// It now travels through Electron main-process memory via the
// electronAPI transfer accessors. These tests drive the real App
// through detach / floating-load / re-attach with an in-memory fake
// of the main-process store, and pin that the localStorage key is
// never written (or read) again.
describe('App floating transfer state (S3-M7)', () => {
  let engineListeners;
  let electronAPI;
  let mainStore; // simulates the main-process transfer variable
  let attachedCallback;
  let detachedCallback;
  let setItemSpy;

  beforeEach(() => {
    window.history.pushState({}, '', '/');
    window.localStorage.clear();
    setItemSpy = jest.spyOn(Storage.prototype, 'setItem');
    engineListeners = [];
    mainStore = null;
    attachedCallback = null;
    detachedCallback = null;
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
      onChatDetached: jest.fn((cb) => { detachedCallback = cb; return jest.fn(); }),
      onChatAttached: jest.fn((cb) => { attachedCallback = cb; return jest.fn(); }),
      openFloatingChat: jest.fn(() => Promise.resolve()),
      closeFloatingChat: jest.fn(() => Promise.resolve()),
      setFloatingTransferState: jest.fn((state) => {
        mainStore = state;
        return Promise.resolve(true);
      }),
      getFloatingTransferState: jest.fn(() => Promise.resolve(mainStore)),
      clearFloatingTransferState: jest.fn(() => {
        mainStore = null;
        return Promise.resolve(true);
      }),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    setItemSpy.mockRestore();
    delete window.electronAPI;
    window.history.pushState({}, '', '/');
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

  const sendChatText = (text) => {
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
  };

  const expectLocalStorageKeyNeverUsed = () => {
    const writes = setItemSpy.mock.calls.filter(([key]) => key === 'floating_transfer_state');
    expect(writes).toEqual([]);
    expect(window.localStorage.getItem('floating_transfer_state')).toBeNull();
  };

  it('detach hands messages + draft input to the main-process store BEFORE opening the floating window, never localStorage', async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    sendChatText('first task');
    expect(screen.getByText('first task')).toBeInTheDocument();

    // A draft typed but not sent must travel with the detach.
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: 'draft in progress' } });

    fireEvent.click(screen.getByTitle('Pop out to floating window'));
    await flushPromises();
    await flushPromises();

    expect(electronAPI.setFloatingTransferState).toHaveBeenCalledTimes(1);
    const handedOver = electronAPI.setFloatingTransferState.mock.calls[0][0];
    expect(handedOver.input).toBe('draft in progress');
    expect(handedOver.messages).toEqual(
      expect.arrayContaining([expect.objectContaining({ role: 'user', content: 'first task' })]),
    );
    expect(mainStore).toEqual(handedOver);

    // Ordering: the floating window must not open before the state
    // exists in main, or it would load an empty/stale history.
    expect(electronAPI.openFloatingChat).toHaveBeenCalledTimes(1);
    expect(electronAPI.setFloatingTransferState.mock.invocationCallOrder[0])
      .toBeLessThan(electronAPI.openFloatingChat.mock.invocationCallOrder[0]);

    expectLocalStorageKeyNeverUsed();
  });

  it('floating window loads its history from the main-process store (not localStorage) and syncs edits back preserving the draft input', async () => {
    mainStore = {
      messages: [
        { role: 'user', content: 'secret history entry' },
        { role: 'agent', content: 'agent reply', isFinal: true },
      ],
      input: 'floating draft',
    };
    window.history.pushState({}, '', '/?floating=true&session=s1');

    render(<App />);
    await flushPromises();
    await flushPromises();

    expect(electronAPI.getFloatingTransferState).toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText('secret history entry')).toBeInTheDocument();
    });
    expect(screen.getByText('agent reply')).toBeInTheDocument();

    // Sync-back: once the loaded messages are in state, the floating
    // window writes them back through the store, and the read-modify-
    // write must preserve the draft input it was handed.
    await waitFor(() => {
      expect(electronAPI.setFloatingTransferState).toHaveBeenCalled();
    });
    const synced = electronAPI.setFloatingTransferState.mock.calls
      .map(([state]) => state)
      .find((state) => state && Array.isArray(state.messages) && state.messages.length === 2);
    expect(synced).toBeDefined();
    expect(synced.input).toBe('floating draft');
    expect(synced.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ content: 'secret history entry' }),
    ]));

    expectLocalStorageKeyNeverUsed();
  });

  it('chat-attached restores the floating edits from the snapshot handed over with the event', async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    sendChatText('original message');
    expect(screen.getByText('original message')).toBeInTheDocument();

    // Main process: floating window closed -> store cleared, final
    // snapshot delivered WITH the chat-attached event.
    await act(async () => {
      attachedCallback({
        messages: [{ role: 'user', content: 'edited inside floating window' }],
        input: '',
      });
    });
    await flushPromises();

    expect(screen.getByText('edited inside floating window')).toBeInTheDocument();
    expect(screen.queryByText('original message')).not.toBeInTheDocument();
    expectLocalStorageKeyNeverUsed();
  });

  it('re-attach button reads the floating edits from the store before closing the floating window', async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    sendChatText('first task');
    fireEvent.click(screen.getByTitle('Pop out to floating window'));
    await flushPromises();
    await flushPromises();
    expect(electronAPI.openFloatingChat).toHaveBeenCalledTimes(1);

    // The floating window edited the history while detached.
    mainStore = {
      messages: [{ role: 'user', content: 'floating edit wins' }],
      input: '',
    };
    electronAPI.getFloatingTransferState.mockClear();

    fireEvent.click(screen.getByLabelText('Re-attach to Main Window'));
    await flushPromises();
    await flushPromises();

    expect(electronAPI.getFloatingTransferState).toHaveBeenCalled();
    expect(electronAPI.closeFloatingChat).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.getByText('floating edit wins')).toBeInTheDocument();
    });
    expectLocalStorageKeyNeverUsed();
  });

  it('App.jsx source never reads or writes the localStorage transfer key', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'App.jsx'), 'utf8');
    expect(source).not.toContain("localStorage.getItem('floating_transfer_state')");
    expect(source).not.toContain("localStorage.setItem('floating_transfer_state')");
    expect(source).not.toContain('localStorage.getItem("floating_transfer_state")');
    expect(source).not.toContain('localStorage.setItem("floating_transfer_state")');
    // The IPC-backed accessors are the replacement surface.
    expect(source).toContain('getFloatingTransferState');
    expect(source).toContain('setFloatingTransferState');
  });
});
