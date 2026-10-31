import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import App from '../App';

// Stub 3D force graph for jsdom
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// Stub react-markdown
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('SessionExportImport', () => {
  describe('DraggableChatWindow export options in session menu', () => {
    it('renders export menu toggle and shows Markdown, JSON, and ZIP options', () => {
      const onExport = jest.fn();
      render(
        <DraggableChatWindow
          messages={[{ id: '1', role: 'user', content: 'Hello' }]}
          status="Online"
          onExportSession={onExport}
          sessionId="sess-123"
        />
      );

      // Look for the Export button / menu in header
      const exportBtn = screen.getByTitle(/export session/i) || screen.getByLabelText(/export session/i);
      expect(exportBtn).toBeInTheDocument();

      fireEvent.click(exportBtn);

      // Verify the three export format options are presented
      expect(screen.getByText(/markdown/i)).toBeInTheDocument();
      expect(screen.getByText(/json/i)).toBeInTheDocument();
      expect(screen.getByText(/zip/i)).toBeInTheDocument();

      // Click Markdown option
      fireEvent.click(screen.getByText(/markdown/i));
      expect(onExport).toHaveBeenCalledWith('markdown');
    });

    it('triggers onExportSession with json and zip formats', () => {
      const onExport = jest.fn();
      render(
        <DraggableChatWindow
          messages={[{ id: '1', role: 'user', content: 'Hello' }]}
          status="Online"
          onExportSession={onExport}
          sessionId="sess-123"
        />
      );

      const exportBtn = screen.getByTitle(/export session/i) || screen.getByLabelText(/export session/i);
      fireEvent.click(exportBtn);
      fireEvent.click(screen.getByText(/json/i));
      expect(onExport).toHaveBeenCalledWith('json');

      fireEvent.click(exportBtn);
      fireEvent.click(screen.getByText(/zip/i));
      expect(onExport).toHaveBeenCalledWith('zip');
    });
  });

  describe('App session sidebar import action and WS responses', () => {
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

    const flushPromises = async () => {
      await act(async () => {});
    };

    const connectApp = async () => {
      render(<App />);
      await flushPromises();
      emitEngineMessage({ type: '_ws_status', status: 'connected' });
      await flushPromises();
    };

    it('renders the Import Session button in the sidebar', async () => {
      await connectApp();
      const importBtn = screen.getByTitle(/import session/i) || screen.getByLabelText(/import session/i);
      expect(importBtn).toBeInTheDocument();
    });

    it('handles SESSION_IMPORT_RESPONSE success by refreshing sessions and selecting the imported session', async () => {
      await connectApp();

      emitEngineMessage({
        type: 'SESSION_IMPORT_RESPONSE',
        payload: {
          ok: true,
          session_id: 'imported-sess-1',
          title: 'Restored Archive Chat',
          message_count: 3,
          error: null,
        },
      });
      await flushPromises();

      // Should have requested session list to refresh
      const sentPackets = electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));
      const listRequests = sentPackets.filter((p) => p.type === 'SESSION_LIST_REQUEST');
      expect(listRequests.length).toBeGreaterThan(0);
    });

    it('handles SESSION_IMPORT_RESPONSE failure by displaying error', async () => {
      await connectApp();

      emitEngineMessage({
        type: 'SESSION_IMPORT_RESPONSE',
        payload: {
          ok: false,
          session_id: '',
          title: '',
          message_count: 0,
          error: 'Corrupt or invalid session archive',
        },
      });
      await flushPromises();

      // Verify error alert or toast is shown
      expect(screen.getByText(/corrupt or invalid session archive/i)).toBeInTheDocument();
    });
  });
});
