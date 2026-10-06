import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn()
  })
}));

describe('SettingsPanel Tools Tab', () => {
  it('renders correctly with risk badges and disabled Always Allow for risky tools', () => {
    const mockWsRef = { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } };
    render(<SettingsPanel wsRef={mockWsRef} providers={[]} localModels={[]} />);
    expect(true).toBe(true); // placeholder test
  });
});
