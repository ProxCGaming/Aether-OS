/** D4-9: the Capabilities tab's capability-issues roll-up. Pins:
 * the three owning lists are requested on mount; issues are
 * counted with the owning panels' own state rules from those
 * same payloads; each count links to its owning tab; a clean
 * set of answers says so instead of inventing a count; and no
 * answer yet renders a load state, never a zero. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import CapabilityIssuesCard from '../CapabilityIssuesCard';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

describe('CapabilityIssuesCard', () => {
  let mockSendMessage;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    const mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') messageListeners.push(cb);
        }),
        removeEventListener: jest.fn((event, cb) => {
          if (event === 'message') {
            messageListeners = messageListeners.filter(l => l !== cb);
          }
        }),
        readyState: 1
      }
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
      isConnected: true,
      reconnectCount: 0
    });
  });

  const deliver = (type, payload) => {
    act(() => {
      messageListeners.forEach(cb => cb({ data: JSON.stringify({ type, payload }) }));
    });
  };

  test('requests the three owning lists on mount and shows a load state until one answers', () => {
    render(<CapabilityIssuesCard onOpenCapabilityTab={jest.fn()} />);
    const sentTypes = mockSendMessage.mock.calls.map(([type]) => type);
    expect(sentTypes).toEqual(expect.arrayContaining([
      'MCP_SERVER_LIST_REQUEST', 'PLUGIN_LIST_REQUEST', 'SKILL_LIST_REQUEST'
    ]));
    expect(screen.getByTestId('capability-issues-load-state')).toBeInTheDocument();
    expect(screen.queryByTestId('capability-issues-clear')).not.toBeInTheDocument();
  });

  test('counts issues with the panels\' own rules and links each to its tab', () => {
    const onOpen = jest.fn();
    render(<CapabilityIssuesCard onOpenCapabilityTab={onOpen} />);
    deliver('MCP_SERVER_LIST_RESPONSE', {
      servers: {
        good: { status: 'connected', error: null },
        broken: { status: 'failed', error: null },
        invalid: { status: 'invalid', error: 'missing command' }
      }
    });
    deliver('PLUGIN_LIST_RESPONSE', { plugins: [], registry_load_error: 'registry is corrupt' });
    deliver('SKILL_LIST_RESPONSE', {
      skills: [
        { name: 'fine', state: 'ok', enabled: true },
        { name: 'broken', state: 'load_error', error: 'frontmatter' }
      ]
    });

    expect(screen.getByText('4 Capability Issues')).toBeInTheDocument();
    expect(screen.getByTestId('capability-issues-mcp')).toHaveTextContent('2 MCP servers failing or invalid');
    expect(screen.getByTestId('capability-issues-plugins')).toHaveTextContent('The plugin registry could not be read');
    expect(screen.getByTestId('capability-issues-skills')).toHaveTextContent('1 skill failed to load');

    fireEvent.click(screen.getByTestId('capability-issues-open-mcp'));
    expect(onOpen).toHaveBeenCalledWith('MCP');
    fireEvent.click(screen.getByTestId('capability-issues-open-plugins'));
    expect(onOpen).toHaveBeenCalledWith('Plugins');
    fireEvent.click(screen.getByTestId('capability-issues-open-skills'));
    expect(onOpen).toHaveBeenCalledWith('Skills');
  });

  test('clean answers render the all-clear, not a zero count', () => {
    render(<CapabilityIssuesCard onOpenCapabilityTab={jest.fn()} />);
    deliver('MCP_SERVER_LIST_RESPONSE', { servers: { good: { status: 'connected', error: null } } });
    deliver('PLUGIN_LIST_RESPONSE', { plugins: [], registry_load_error: null });
    deliver('SKILL_LIST_RESPONSE', { skills: [{ name: 'fine', state: 'ok', enabled: true }] });

    expect(screen.getByText('No Capability Issues')).toBeInTheDocument();
    expect(screen.getByTestId('capability-issues-clear')).toHaveTextContent('loaded cleanly');
    expect(screen.queryByTestId('capability-issues-mcp')).not.toBeInTheDocument();
  });

  test('a disabled MCP server is not an issue; an error-carrying one is', () => {
    render(<CapabilityIssuesCard onOpenCapabilityTab={jest.fn()} />);
    deliver('MCP_SERVER_LIST_RESPONSE', {
      servers: {
        off: { status: 'disabled', error: null, enabled: false },
        noisy: { status: 'connected', error: 'handshake warning' }
      }
    });
    expect(screen.getByTestId('capability-issues-mcp')).toHaveTextContent('1 MCP server failing or invalid');
  });
});
