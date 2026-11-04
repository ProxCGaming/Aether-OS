import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../contexts/SocketContext';
import MediaLibrary from '../components/MediaLibrary';
import LocalModelsTab from '../LocalModelsTab';
import SettingsPanel from '../SettingsPanel';

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeElectronAPI({ connected = false } = {}) {
  const listeners = [];
  const api = {
    sendEngineMessage: jest.fn(),
    onEngineMessage: jest.fn((cb) => {
      listeners.push(cb);
      return () => {
        const idx = listeners.indexOf(cb);
        if (idx !== -1) listeners.splice(idx, 1);
      };
    }),
    getWsStatus: jest.fn(() => Promise.resolve(connected)),
  };
  return { api, listeners };
}

// Wires the components to the REAL SocketContext exactly the way
// App does: the facade wsRef plus the safeSend / pendingMessages
// props (SettingsPanel forwards them to LocalModelsTab itself).
function MediaHarness({ captureRef, items }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <MediaLibrary
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      initialMedia={items}
      sessions={[]}
      onNavigateToSession={() => {}}
    />
  );
}

function HubHarness({ captureRef }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <LocalModelsTab
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
    />
  );
}

function SettingsHarness({ captureRef, initialMenu }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <SettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      providers={[]}
      localModels={[]}
      initialMenu={initialMenu}
    />
  );
}

const MEDIA_ITEM = {
  id: 'med-1',
  file_path: '/pics/login.png',
  media_type: 'image',
  origin: 'user_upload',
  collection: 'user',
  tags: ['login'],
  user_notes: 'Original notes',
  session_id: 'sess-1',
  file_size: 100,
  created_at: 1000,
};

const OLLAMA_ROW = {
  id: 'llama3',
  name: 'Llama 3',
  source: 'ollama',
  runnability: 'ollama',
  family: 'brain',
  description: 'A local brain.',
};

// Wave 2 slice 3b: the MediaLibrary, Local Models and diagnostics
// action sites from Lane A — RUX-A-09 (media save/delete fake-applied
// offline), RUX-A-19 (Local Models tab: eternal search spinner,
// cancel that lied, discarded set-active) and RUX-A-13 (diagnostics /
// backup / restore / update-check / export sends discarded behind
// pending states that never resolved). Every one now goes through
// safeSend: queued offline with an honest marking derived from the
// real queue, applied for real when the reply lands after flush.
describe('Media / hub / diagnostics safeSend conversions (Wave 2 slice 3b)', () => {
  let electronAPI;
  let listeners;
  let captureRef;

  function setup({ connected = false } = {}) {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected }));
    window.electronAPI = electronAPI;
    captureRef = { current: null };
  }

  afterEach(() => {
    delete window.electronAPI;
    jest.restoreAllMocks();
  });

  async function flushPromises() {
    await act(async () => {});
  }

  const emitEngineMessage = (raw) => {
    act(() => {
      [...listeners].forEach((cb) => cb(raw));
    });
  };

  const feed = (type, payload, requestId) => emitEngineMessage(JSON.stringify({
    type,
    payload,
    ...(requestId ? { request_id: requestId } : {}),
  }));

  const sentOfType = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
    .filter((p) => p && p.type === type);

  const queuedOfType = (type) => captureRef.current.pendingMessages
    .filter((m) => m.type === type);

  const connect = async () => {
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
  };

  const disconnect = async () => {
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'disconnected' }));
    await flushPromises();
  };

  // ---- RUX-A-09: MediaLibrary -----------------------------------

  test('A-09: offline delete keeps the item visible with a queued marking; flush + reply removes it', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <MediaHarness captureRef={captureRef} items={[MEDIA_ITEM]} />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByTestId('media-delete-btn-med-1'));
    expect(screen.getByTestId('delete-confirm-modal')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));

    // NOT gone-as-final: the card is still there, marked queued,
    // its delete button inert, and the request sits in the real
    // queue instead of having been sent or discarded.
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-delete-queued-med-1')).toHaveTextContent('Delete queued');
    expect(screen.getByTestId('media-delete-btn-med-1')).toBeDisabled();
    expect(sentOfType('MEDIA_DELETE_REQUEST')).toHaveLength(0);
    expect(queuedOfType('MEDIA_DELETE_REQUEST')).toHaveLength(1);
    expect(queuedOfType('MEDIA_DELETE_REQUEST')[0].payload.media_id).toBe('med-1');

    // Reconnect: the queued delete flushes; the reply removes the
    // item for real.
    await connect();
    expect(sentOfType('MEDIA_DELETE_REQUEST')).toHaveLength(1);
    feed('MEDIA_DELETE_RESPONSE', { media_id: 'med-1', deleted: true, file_deleted: false });
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();
  });

  test('A-09: offline metadata save applies nothing locally; the reply applies it after flush', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <MediaHarness captureRef={captureRef} items={[MEDIA_ITEM]} />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByTestId('media-card-med-1'));
    fireEvent.change(screen.getByTestId('media-notes-textarea'), { target: { value: 'Queued edit notes' } });
    fireEvent.click(screen.getByTestId('media-save-metadata-btn'));

    // The edit is queued, not saved: the card carries the marking,
    // the queue holds the request, and reopening the editor still
    // shows the OLD notes — nothing was fake-applied.
    expect(screen.getByTestId('media-meta-queued-med-1')).toHaveTextContent('Edit queued');
    expect(queuedOfType('MEDIA_METADATA_UPDATE_REQUEST')).toHaveLength(1);
    expect(queuedOfType('MEDIA_METADATA_UPDATE_REQUEST')[0].payload.notes).toBe('Queued edit notes');
    fireEvent.click(screen.getByTestId('media-card-med-1'));
    expect(screen.getByTestId('media-notes-textarea')).toHaveValue('Original notes');
    fireEvent.click(screen.getByTestId('media-modal-close-btn'));

    await connect();
    expect(sentOfType('MEDIA_METADATA_UPDATE_REQUEST')).toHaveLength(1);
    feed('MEDIA_METADATA_UPDATE_RESPONSE', { media_id: 'med-1', tags: ['login'], notes: 'Queued edit notes' });

    fireEvent.click(screen.getByTestId('media-card-med-1'));
    expect(screen.getByTestId('media-notes-textarea')).toHaveValue('Queued edit notes');
  });

  test('A-09: connected save and delete behave as before (optimistic, sent immediately)', async () => {
    setup({ connected: true });
    render(
      <SocketProvider>
        <MediaHarness captureRef={captureRef} items={[MEDIA_ITEM]} />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByTestId('media-card-med-1'));
    fireEvent.change(screen.getByTestId('media-notes-textarea'), { target: { value: 'Live notes' } });
    fireEvent.click(screen.getByTestId('media-save-metadata-btn'));
    expect(sentOfType('MEDIA_METADATA_UPDATE_REQUEST')).toHaveLength(1);
    expect(sentOfType('MEDIA_METADATA_UPDATE_REQUEST')[0].payload.notes).toBe('Live notes');
    expect(queuedOfType('MEDIA_METADATA_UPDATE_REQUEST')).toHaveLength(0);

    fireEvent.click(screen.getByTestId('media-delete-btn-med-1'));
    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));
    expect(sentOfType('MEDIA_DELETE_REQUEST')).toHaveLength(1);
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();
  });

  // ---- RUX-A-19: Local Models tab --------------------------------

  test('A-19: offline search ends its spinner with a queued outcome; the reply completes it after flush', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <HubHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();

    expect(screen.getByTestId('hub-search-queued')).toHaveTextContent('Search queued');
    expect(screen.queryByText(/Searching/)).not.toBeInTheDocument();
    expect(queuedOfType('MODEL_HUB_SEARCH_REQUEST')).toHaveLength(1);

    await connect();
    const searchReq = sentOfType('MODEL_HUB_SEARCH_REQUEST')[0];
    expect(searchReq).toBeTruthy();
    feed('MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null }, searchReq.request_id);

    expect(screen.queryByTestId('hub-search-queued')).not.toBeInTheDocument();
    expect(screen.getByTestId('result-row-ollama:llama3')).toBeInTheDocument();
  });

  test('A-19: offline cancel keeps the progress UI and queues; TASK_CANCELLED clears it after flush', async () => {
    setup({ connected: true });
    render(
      <SocketProvider>
        <HubHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();

    const searchReq = sentOfType('MODEL_HUB_SEARCH_REQUEST')[0];
    feed('MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null }, searchReq.request_id);
    fireEvent.click(screen.getByRole('button', { name: /Download/ }));
    const downloadReq = sentOfType('MODEL_HUB_DOWNLOAD_REQUEST')[0];
    feed('MODEL_HUB_DOWNLOAD_RESPONSE', { success: true, model: 'llama3' }, downloadReq.request_id);
    expect(screen.getByTitle('Cancel Download')).toBeInTheDocument();

    await disconnect();
    fireEvent.click(screen.getByTitle('Cancel Download'));

    // The dangerous lie is gone: progress is still on screen, the
    // cancel is marked queued, and nothing was sent or discarded.
    expect(screen.getByTestId('cancel-queued-llama3')).toHaveTextContent('Cancel queued');
    expect(queuedOfType('LOCAL_MODEL_DOWNLOAD_CANCEL')).toHaveLength(1);
    expect(queuedOfType('LOCAL_MODEL_DOWNLOAD_CANCEL')[0].payload.model).toBe('llama3');
    expect(sentOfType('LOCAL_MODEL_DOWNLOAD_CANCEL')).toHaveLength(0);

    await connect();
    expect(sentOfType('LOCAL_MODEL_DOWNLOAD_CANCEL')).toHaveLength(1);
    feed('TASK_CANCELLED', { model: 'llama3' });
    expect(screen.queryByTestId('cancel-queued-llama3')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Cancel Download')).not.toBeInTheDocument();
  });

  test('A-19: offline download shows a queued state instead of silently doing nothing', async () => {
    setup({ connected: true });
    render(
      <SocketProvider>
        <HubHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();

    const searchReq = sentOfType('MODEL_HUB_SEARCH_REQUEST')[0];
    feed('MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null }, searchReq.request_id);

    await disconnect();
    fireEvent.click(screen.getByRole('button', { name: /Download/ }));

    expect(screen.getByTestId('download-queued-ollama:llama3')).toHaveTextContent('Download queued');
    expect(queuedOfType('MODEL_HUB_DOWNLOAD_REQUEST')).toHaveLength(1);
    expect(sentOfType('MODEL_HUB_DOWNLOAD_REQUEST')).toHaveLength(0);

    await connect();
    const downloadReq = sentOfType('MODEL_HUB_DOWNLOAD_REQUEST')[0];
    expect(downloadReq).toBeTruthy();
    feed('MODEL_HUB_DOWNLOAD_RESPONSE', { success: true, model: 'llama3' }, downloadReq.request_id);
    expect(screen.queryByTestId('download-queued-ollama:llama3')).not.toBeInTheDocument();
    expect(screen.getByTitle('Cancel Download')).toBeInTheDocument();
  });

  test('A-19: offline set-active queues like the other model-switch paths; the broadcast confirms it', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <HubHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();

    await connect();
    const listReq = sentOfType('MODEL_HUB_DOWNLOADED_REQUEST')[0];
    feed('MODEL_HUB_DOWNLOADED_RESPONSE', { models: [OLLAMA_ROW] }, listReq.request_id);
    expect(screen.getByTestId('downloaded-row-ollama:llama3')).toBeInTheDocument();

    await disconnect();
    fireEvent.click(screen.getByRole('button', { name: 'Set Active' }));

    expect(screen.getByTestId('set-active-queued-ollama:llama3')).toBeInTheDocument();
    expect(queuedOfType('MODEL_SET_DEFAULT')).toHaveLength(1);
    expect(queuedOfType('MODEL_SET_DEFAULT')[0].payload).toMatchObject({ provider: 'ollama', model: 'llama3' });
    expect(sentOfType('MODEL_SET_DEFAULT')).toHaveLength(0);

    await connect();
    expect(sentOfType('MODEL_SET_DEFAULT')).toHaveLength(1);
    feed('MODEL_DEFAULT_CHANGED', { provider: 'ollama', model: 'llama3' });
    expect(screen.getByRole('button', { name: 'Active' })).toBeInTheDocument();
  });

  test('A-19: offline hub delete queues with a marking; flush + reply removes the row', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <HubHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();

    await connect();
    const listReq = sentOfType('MODEL_HUB_DOWNLOADED_REQUEST')[0];
    feed('MODEL_HUB_DOWNLOADED_RESPONSE', { models: [OLLAMA_ROW] }, listReq.request_id);
    expect(screen.getByTestId('downloaded-row-ollama:llama3')).toBeInTheDocument();

    await disconnect();
    fireEvent.click(screen.getByTitle('Delete Model'));

    expect(screen.getByTestId('hub-delete-queued-ollama:llama3')).toHaveTextContent('Delete queued');
    expect(queuedOfType('MODEL_HUB_DELETE_REQUEST')).toHaveLength(1);
    expect(screen.getByTestId('downloaded-row-ollama:llama3')).toBeInTheDocument();

    await connect();
    const deleteReq = sentOfType('MODEL_HUB_DELETE_REQUEST')[0];
    expect(deleteReq).toBeTruthy();
    feed('MODEL_HUB_DELETE_RESPONSE', { success: true }, deleteReq.request_id);
    // The success handler refreshes the inventory; answer it empty.
    const refreshReq = sentOfType('MODEL_HUB_DOWNLOADED_REQUEST')
      .filter((r) => r.request_id !== listReq.request_id)[0];
    feed('MODEL_HUB_DOWNLOADED_RESPONSE', { models: [] }, refreshReq.request_id);
    expect(screen.queryByTestId('downloaded-row-ollama:llama3')).not.toBeInTheDocument();
  });

  // ---- RUX-A-13: diagnostics / backup family ---------------------

  test('A-13: offline backup create queues with an honest status; the reply resolves it after flush', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <SettingsHarness captureRef={captureRef} initialMenu="Advanced" />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: 'Create backup' }));

    expect(screen.getByText('Backup queued — will run when the engine reconnects.')).toBeInTheDocument();
    expect(screen.queryByText('Creating backup…')).not.toBeInTheDocument();
    expect(queuedOfType('BACKUP_CREATE_REQUEST')).toHaveLength(1);
    expect(sentOfType('BACKUP_CREATE_REQUEST')).toHaveLength(0);

    await connect();
    expect(sentOfType('BACKUP_CREATE_REQUEST')).toHaveLength(1);
    feed('BACKUP_CREATE_RESPONSE', { success: true, path: '/ws/AetherBackups/b.zip', files: 3 });
    expect(screen.getByText('Backup saved: /ws/AetherBackups/b.zip (3 files)')).toBeInTheDocument();
  });

  test('A-13: offline update check queues with an honest status; the reply resolves it after flush', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <SettingsHarness captureRef={captureRef} initialMenu="Advanced" />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));

    expect(screen.getByText('Update check queued — will run when the engine reconnects.')).toBeInTheDocument();
    expect(screen.queryByText('Checking…')).not.toBeInTheDocument();
    expect(queuedOfType('VERSION_CHECK_REQUEST')).toHaveLength(1);

    await connect();
    expect(sentOfType('VERSION_CHECK_REQUEST')).toHaveLength(1);
    feed('VERSION_CHECK_RESPONSE', { success: true, update_available: false, current: '1.0.0' });
    expect(screen.getByText("You're on the latest version (1.0.0).")).toBeInTheDocument();
  });

  test('A-13: offline diagnostics run queues inert with a note — no eternal Testing state', async () => {
    setup({ connected: false });
    render(
      <SocketProvider>
        <SettingsHarness captureRef={captureRef} initialMenu="Capabilities" />
      </SocketProvider>
    );
    await flushPromises();

    fireEvent.click(screen.getByRole('button', { name: 'Run Diagnostics Now' }));

    expect(screen.getByTestId('diagnostics-run-queued')).toHaveTextContent('Diagnostics run queued');
    expect(screen.getByRole('button', { name: 'Run Queued' })).toBeDisabled();
    expect(screen.queryByText('Testing...')).not.toBeInTheDocument();
    expect(queuedOfType('ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST')).toHaveLength(1);
    expect(sentOfType('ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST')).toHaveLength(0);

    await connect();
    expect(sentOfType('ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST')).toHaveLength(1);
    feed('ENVIRONMENT_DIAGNOSTIC_RESPONSE', { summary: { failed_count: 0 }, history: [] });
    expect(screen.queryByTestId('diagnostics-run-queued')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run Diagnostics Now' })).toBeInTheDocument();
  });
});
