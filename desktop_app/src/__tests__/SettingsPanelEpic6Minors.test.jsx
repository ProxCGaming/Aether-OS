import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import fs from 'fs';
import path from 'path';
import SettingsPanel from '../SettingsPanel';
import pkg from '../../package.json';

// Epic 6 minors, SettingsPanel seams:
//  - E6-F14: an About section shows the app version (read from the
//    real version source — the desktop package manifest, pinned equal
//    to the engine's APP_VERSION) and offers the bundled local guide.
//  - E6-F15: ordinary offline edits were silently dropped by
//    readyState guards in the panel's own handlers — the facade's
//    send() already queues while offline and flushes on reconnect
//    (the E6-F02 discipline), so the guards bypassed the queue.
//  - P-1 (E6-F07 flag): CONFIG_RESET_RESPONSE never checked success —
//    a save-failed reset disarmed and showed "done".
//  - P-2 (E7-F05 follow-up): PLUGIN_*_RESPONSE failures showed
//    nothing in this panel (DiagnosticsPanel surfaced them; the
//    parent panel swallowed them).
//  - E6-F16: the settings search input suppressed the focus ring.

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

// A wsRef implementing the SocketContext facade's contract: send()
// delivers while open and QUEUES while closed, flushing in order on
// reopen. That queue is what an offline edit must reach.
function makeFacadeWsRef() {
  let messageHandler = null;
  let open = true;
  const queued = [];
  const delivered = [];
  const socket = {
    get readyState() { return open ? 1 : 3; },
    send: jest.fn((raw) => {
      if (open) delivered.push(raw);
      else queued.push(raw);
    }),
    addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
    removeEventListener: jest.fn(),
  };
  return {
    wsRef: { current: socket },
    socket,
    delivered,
    getHandler: () => messageHandler,
    setOpen: (value) => {
      open = value;
      if (open) {
        while (queued.length) delivered.push(queued.shift());
      }
    },
  };
}

const deliveredTypes = (delivered) => delivered.map((raw) => JSON.parse(raw).type);
const deliveredOfType = (delivered, type) =>
  delivered.map((raw) => JSON.parse(raw)).filter((m) => m.type === type);

describe('SettingsPanel Epic 6 minors', () => {
  it('P-1: a failed reset surfaces the error and never shows "done"', () => {
    const { wsRef, getHandler } = makeFacadeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    fireEvent.click(screen.getByText('Click again to confirm reset'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'CONFIG_RESET_RESPONSE',
          payload: { success: false, error: 'Could not save the reset settings.' },
        }),
      });
    });
    expect(screen.queryByText('Settings were reset to defaults.')).not.toBeInTheDocument();
    expect(screen.getByText(/Reset failed: Could not save the reset settings\./)).toBeInTheDocument();
    // Disarmed: the two-step button is offered again.
    expect(screen.getByRole('button', { name: 'Reset all settings' })).toBeInTheDocument();
  });

  it('P-1 control: a successful reset still confirms as before', () => {
    const { wsRef, getHandler } = makeFacadeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    fireEvent.click(screen.getByText('Click again to confirm reset'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'CONFIG_RESET_RESPONSE',
          payload: { success: true, config: {} },
        }),
      });
    });
    expect(screen.getByText('Settings were reset to defaults.')).toBeInTheDocument();
  });

  it('P-2: a failed plugin action is surfaced in the panel', () => {
    const { wsRef, getHandler } = makeFacadeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Plugins'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'PLUGIN_UNINSTALL_RESPONSE',
          payload: { success: false, name: 'demo-plugin', error: 'Plugin is still connected to an MCP server.' },
        }),
      });
    });
    expect(screen.getByText(/Uninstall demo-plugin failed: Plugin is still connected to an MCP server\./)).toBeInTheDocument();
  });

  it('E6-F15: an offline Advanced edit is queued and submitted on reconnect', () => {
    const facade = makeFacadeWsRef();
    render(<SettingsPanel wsRef={facade.wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    facade.setOpen(false);

    // The "Disable Fallback System" toggle is the checkbox inside
    // the row carrying that label.
    const row = screen.getByText('Disable Fallback System').parentElement.parentElement;
    const toggle = row.querySelector('input[type="checkbox"]');
    fireEvent.click(toggle);
    // The edit applied locally…
    expect(toggle).toBeChecked();
    // …but nothing reached the "engine" while offline.
    expect(deliveredOfType(facade.delivered, 'CONFIG_UPDATE_REQUEST')).toHaveLength(0);

    facade.setOpen(true);
    const updates = deliveredOfType(facade.delivered, 'CONFIG_UPDATE_REQUEST');
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config).toEqual({ disable_fallbacks: true });
  });

  it('E6-F15: a reset confirmed while offline is submitted on reconnect', () => {
    const facade = makeFacadeWsRef();
    render(<SettingsPanel wsRef={facade.wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    facade.setOpen(false);
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    fireEvent.click(screen.getByText('Click again to confirm reset'));
    expect(deliveredTypes(facade.delivered)).not.toContain('CONFIG_RESET_REQUEST');
    facade.setOpen(true);
    expect(deliveredTypes(facade.delivered)).toContain('CONFIG_RESET_REQUEST');
  });

  it('E6-F15: a reasoning-effort change while offline is submitted on reconnect', () => {
    const facade = makeFacadeWsRef();
    render(<SettingsPanel wsRef={facade.wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Models & Tasks'));
    facade.setOpen(false);
    // The Reasoning Effort control is a custom Dropdown: open it and
    // pick "Deep Thinking" (the 'high' value).
    fireEvent.click(screen.getByText('Standard'));
    fireEvent.click(screen.getByText('Deep Thinking'));
    expect(deliveredOfType(facade.delivered, 'REASONING_EFFORT_SET_REQUEST')).toHaveLength(0);
    facade.setOpen(true);
    const sent = deliveredOfType(facade.delivered, 'REASONING_EFFORT_SET_REQUEST');
    expect(sent).toHaveLength(1);
    expect(sent[0].payload.effort).toBe('high');
  });

  it('E6-F14: the About section shows the real app version', () => {
    const { wsRef } = makeFacadeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('About'));
    expect(screen.getByText('About Aether-OS')).toBeInTheDocument();
    expect(screen.getByText(`Version ${pkg.version}`)).toBeInTheDocument();
  });

  it('E6-F14: the About section opens the local guide from a workspace root', async () => {
    const { wsRef, getHandler } = makeFacadeWsRef();
    window.electronAPI = { openPath: jest.fn(() => Promise.resolve('')) };
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('About'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: {
            workspaces: [
              { path: '/home/user/Aether-OS', name: 'Aether-OS', is_active: true, exists: true },
            ],
          },
        }),
      });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Open the system guide' }));
    expect(window.electronAPI.openPath).toHaveBeenCalledWith(
      '/home/user/Aether-OS/docs/AETHER_SYSTEM_GUIDE.md');
    delete window.electronAPI;
  });

  it('E6-F16: the settings search input carries the shared focus-ring class', () => {
    const { wsRef } = makeFacadeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    const search = screen.getByLabelText('Search settings');
    expect(search.classList.contains('aether-search-input')).toBe(true);
    expect(search.style.outline).toBe('');
  });
});

describe('E6-F16 stylesheet contract', () => {
  it('index.css gives .aether-search-input the standard focus-visible ring', () => {
    const css = fs.readFileSync(path.join(__dirname, '..', 'index.css'), 'utf8');
    expect(css).toMatch(/\.aether-search-input:focus-visible\s*\{[^}]*outline:\s*2px solid #a78bfa/);
  });
});
