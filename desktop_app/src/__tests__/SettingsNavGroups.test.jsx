/**
 * S-6 (FF1-08): the Settings nav is grouped Personal / Connect /
 * System per the approved Batch 2 shell, and a search that
 * matches nothing renders the "No sections match" state with a
 * clear-search action instead of a blank nav.
 */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

function makeWsRef() {
  return {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    },
  };
}

function renderPanel() {
  return render(<SettingsPanel wsRef={makeWsRef()} providers={[]} localModels={[]} />);
}

// The sidebar's text in render order: group headers and section
// labels interleaved.
const NAV_TEXTS = [
  'Personal', 'Connect', 'System',
  'Memory & Storage', 'Voice Tuning', 'Usage', 'Password Vault', 'Calendar & Reminders', 'Conversation',
  'Models & Tasks', 'API Providers', 'MCP Servers',
  'Agents', 'Tools', 'Skills', 'Plugins', 'Workspace Indexing', 'Browser Agent', 'Computer Use', 'Capabilities', 'Advanced', 'About',
];
function navOrder() {
  const sidebar = screen.getByText('Password Vault').parentElement;
  return Array.from(sidebar.querySelectorAll('div'))
    .map((el) => el.textContent)
    .filter((t) => NAV_TEXTS.includes(t));
}

describe('Settings nav grouping (S-6)', () => {
  it('renders the three groups in board order, each section under its group', () => {
    renderPanel();
    expect(navOrder()).toEqual([
      'Personal',
      'Memory & Storage', 'Voice Tuning', 'Usage', 'Password Vault', 'Calendar & Reminders', 'Conversation',
      'Connect',
      'Models & Tasks', 'API Providers', 'MCP Servers',
      'System',
      'Agents', 'Tools', 'Skills', 'Plugins', 'Workspace Indexing', 'Browser Agent', 'Computer Use', 'Capabilities', 'Advanced', 'About',
    ]);
  });

  it('every one of the 19 existing sections is present exactly once in the nav', () => {
    renderPanel();
    // Scoped to the sidebar: a section's label can also appear in
    // its own panel's heading (e.g. 'API Providers').
    const sidebar = screen.getByText('Password Vault').parentElement;
    const labels = [
      'Memory & Storage', 'Voice Tuning', 'Usage', 'Password Vault', 'Calendar & Reminders', 'Conversation',
      'Models & Tasks', 'API Providers', 'MCP Servers',
      'Agents', 'Tools', 'Skills', 'Plugins', 'Workspace Indexing', 'Browser Agent', 'Computer Use', 'Capabilities', 'Advanced', 'About',
    ];
    for (const label of labels) {
      expect(within(sidebar).getAllByText(label)).toHaveLength(1);
    }
    // No faked Profile section — it is built in Wave 6.
    expect(screen.queryByText('Profile')).not.toBeInTheDocument();
  });

  it('a search with no match renders the no-sections-match state; clear search restores the nav', () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'zzzzqqq' } });
    expect(screen.getByTestId('settings-search-no-results')).toBeInTheDocument();
    expect(screen.getByText('No sections match')).toBeInTheDocument();
    expect(screen.queryByText('Memory & Storage')).not.toBeInTheDocument();
    // Group headers hide too when nothing under them is visible.
    expect(screen.queryByText('Personal')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Clear search'));
    expect(screen.getByText('Memory & Storage')).toBeInTheDocument();
    expect(screen.getByText('Personal')).toBeInTheDocument();
    expect(screen.queryByTestId('settings-search-no-results')).not.toBeInTheDocument();
  });

  it('a matching search keeps the group header of the group that still has items', () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'voice' } });
    expect(screen.getByText('Voice Tuning')).toBeInTheDocument();
    expect(screen.getByText('Personal')).toBeInTheDocument();
    expect(screen.queryByText('Connect')).not.toBeInTheDocument();
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });
});
