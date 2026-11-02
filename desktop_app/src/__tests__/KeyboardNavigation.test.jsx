import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

// E6-F06: core app navigation and Settings section navigation were
// mouse-only DIVs — no tabIndex, no button role, no keyboard handler —
// so they could not be focused or activated from the keyboard, and
// Enter in the Settings search did not jump to a section. These tests
// pin the keyboard contract at the audit's exact seams: focusability,
// Enter/Space activation identical to click, and search-Enter jumping
// to the first matching section. Mouse behaviour is pinned as a
// control so the fix cannot change it.

// Settings children read the socket through the context; stub it (the
// same harness shape as SettingsPanel.test.jsx).
jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

// jsdom has no WebSocket; SettingsPanel only needs the OPEN constant.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeWsRef() {
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    },
  };
  return wsRef;
}

describe('Settings section navigation is keyboard-operable (E6-F06)', () => {
  const renderSettings = () => {
    const wsRef = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    const nav = within(screen.getByLabelText('Search settings').parentElement);
    return { wsRef, nav };
  };

  it('a filtered section item (Advanced) is focusable and Enter opens it', () => {
    const { nav } = renderSettings();

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'advanced' } });
    expect(nav.queryByText('API Providers')).not.toBeInTheDocument();

    const item = nav.getByText('Advanced');
    // Audit repro shape: rendered as DIV / tabIndex -1, focus failed,
    // Enter did not navigate.
    expect(item).toHaveAttribute('role', 'button');
    expect(item.tabIndex).toBe(0);
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(item, { key: 'Enter' });
    expect(screen.getByText('Advanced Settings')).toBeInTheDocument();
  });

  it('Space activates a Settings section item exactly like a click', () => {
    const { nav } = renderSettings();

    const item = nav.getByText('Advanced');
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(item, { key: ' ' });
    expect(screen.getByText('Advanced Settings')).toBeInTheDocument();
  });

  it('Enter in the Settings search jumps to the first matching section', () => {
    renderSettings();

    const search = screen.getByLabelText('Search settings');
    fireEvent.change(search, { target: { value: 'voice' } });
    // Filtered to Voice Tuning only; nothing activated yet.
    expect(screen.queryByText('Fine-tune speech detection and endpointing thresholds. Changes take effect on the next voice session.')).not.toBeInTheDocument();

    fireEvent.keyDown(search, { key: 'Enter' });
    expect(screen.getByText('Fine-tune speech detection and endpointing thresholds. Changes take effect on the next voice session.')).toBeInTheDocument();
  });

  it('Enter in the Settings search with no matches changes nothing', () => {
    renderSettings();

    const search = screen.getByLabelText('Search settings');
    fireEvent.change(search, { target: { value: 'zzzz-no-match' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    // Still on the default Providers section; no crash, no jump.
    expect(screen.queryByText('Advanced Settings')).not.toBeInTheDocument();
    expect(screen.queryByText('Fine-tune speech detection and endpointing thresholds. Changes take effect on the next voice session.')).not.toBeInTheDocument();
  });

  it('control: mouse click still opens a Settings section', () => {
    const { nav } = renderSettings();

    fireEvent.click(nav.getByText('Advanced'));
    expect(screen.getByText('Advanced Settings')).toBeInTheDocument();
  });
});
