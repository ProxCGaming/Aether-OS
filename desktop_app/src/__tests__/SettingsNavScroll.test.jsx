/**
 * Settings nav scroll pin (13 Nov 2026, owner report): the Settings
 * sidebar had no overflow handling, so at shorter window heights
 * every section below Password Vault — Memory & Storage,
 * Capabilities, Voice Tuning, Advanced, About — was clipped by the
 * panel and could not be scrolled to or clicked. The nav column
 * must scroll itself, with items at their natural height.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
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

describe('Settings sidebar navigation', () => {
  it('scrolls itself, and every section below Password Vault is in it', () => {
    render(<SettingsPanel wsRef={makeWsRef()} providers={[]} localModels={[]} />);

    const vaultItem = screen.getByText('Password Vault');
    const sidebar = vaultItem.parentElement;

    expect(sidebar.style.overflowY).toBe('auto');
    expect(sidebar.className).toContain('chat-scroll');
    // Items keep their natural height instead of compressing.
    expect(vaultItem.style.flexShrink).toBe('0');

    for (const label of ['Memory & Storage', 'Capabilities', 'Voice Tuning', 'Advanced', 'About']) {
      const item = screen.getByText(label);
      expect(item.parentElement).toBe(sidebar);
    }
  });
});
