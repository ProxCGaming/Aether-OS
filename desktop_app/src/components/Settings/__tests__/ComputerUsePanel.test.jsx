/**
 * ComputerUsePanel tests (S19 → S20).
 *
 * S19 pinned an observe-only panel whose Control toggle was
 * disabled pure UI state. S20 supersedes exactly those pins:
 * Control is now a REAL stored field (computer_use_control)
 * behind a one-time acknowledgement (computer_use_control_ack),
 * and the panel grows per-category confirmation modes, per-app
 * allow/deny rules, the pixel-fallback and envelopes switches,
 * and the remembered-grants list with Revoke (fetched over
 * DESKTOP_GRANTS_* on the socket, the BrowserPolicyPanel
 * pattern). The S19 pins that still hold are kept: the
 * master/observe reporting, the closed vision-mode set, the
 * read-only inherited approval mode, the load state, and the
 * verbatim config error.
 *
 * The socket is mocked (the panel calls useAetherSocket
 * unconditionally) and a fake wsRef is passed as a prop, exactly
 * like BrowserPolicyPanel.test.jsx drives its panel.
 */
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import ComputerUsePanel from '../ComputerUsePanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeWsRef() {
  let messageListeners = [];
  const wsRef = {
    current: {
      readyState: 1, // WebSocket.OPEN
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  const simulateMessage = (data) => {
    messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
  };
  useAetherSocket.mockReturnValue({ wsRef: null, sendMessage: null });
  return { wsRef, simulateMessage };
}

const baseProps = {
  configAnswered: true,
  isConnected: true,
  configError: null,
  computerUseEnabled: false,
  onComputerUseEnabledChange: jest.fn(),
  computerUseObserve: false,
  onComputerUseObserveChange: jest.fn(),
  visionCapture: 'off',
  onVisionCaptureChange: jest.fn(),
  computerUseControl: false,
  onComputerUseControlChange: jest.fn(),
  computerUseControlAck: false,
  onComputerUseControlAckChange: jest.fn(),
  computerUseCategoryModes: {},
  onComputerUseCategoryModeChange: jest.fn(),
  computerUseAppRules: [],
  onComputerUseAppRulesChange: jest.fn(),
  computerUseEnvelopesEnabled: true,
  onComputerUseEnvelopesEnabledChange: jest.fn(),
  computerUsePixelFallback: false,
  onComputerUsePixelFallbackChange: jest.fn(),
  approvalMode: 'ask',
};

const renderPanel = (overrides = {}) => {
  const { wsRef, simulateMessage } = makeWsRef();
  const props = { ...baseProps, ...overrides, wsRef };
  render(<ComputerUsePanel {...props} />);
  const sent = () =>
    wsRef.current.send.mock.calls.map((call) => JSON.parse(call[0]));
  return { props, wsRef, simulateMessage, sent };
};

describe('ComputerUsePanel (S20)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders the S19 carry-overs: switches, closed vision set, inherited approval', () => {
    renderPanel();
    expect(screen.getByTestId('computer-use-enabled-toggle')).not.toBeChecked();
    expect(screen.getByTestId('computer-use-observe-toggle')).not.toBeChecked();
    expect(screen.getByTestId('computer-use-vision-capture')).toHaveValue('off');
    const options = Array.from(
      screen.getByTestId('computer-use-vision-capture').options
    ).map((option) => option.value);
    expect(options).toEqual(['off', 'ask', 'on_demand']);
    expect(screen.getByTestId('computer-use-approval-mode')).toHaveTextContent(
      'Ask for approval'
    );
    // Observe copy still tells the truth: accessibility TEXT only.
    expect(screen.getByText(/accessibility TEXT only/)).toBeInTheDocument();
  });

  test('master and observe edits report through the config path props', () => {
    const { props } = renderPanel({ computerUseEnabled: true });
    fireEvent.click(screen.getByTestId('computer-use-observe-toggle'));
    expect(props.onComputerUseObserveChange).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByTestId('computer-use-enabled-toggle'));
    expect(props.onComputerUseEnabledChange).toHaveBeenCalledWith(false);
    fireEvent.change(screen.getByTestId('computer-use-vision-capture'), {
      target: { value: 'on_demand' },
    });
    expect(props.onVisionCaptureChange).toHaveBeenCalledWith('on_demand');
  });

  test('before the config answer the load state shows instead of values', () => {
    renderPanel({ configAnswered: false });
    expect(screen.getByTestId('computer-use-config-load-state')).toHaveTextContent(
      'Loading your saved computer use settings…'
    );
    expect(screen.queryByTestId('computer-use-enabled-toggle')).not.toBeInTheDocument();
  });

  test('a failed config load is an error state with a retry, not endless loading', () => {
    const onRetryConfigLoad = jest.fn();
    renderPanel({ configAnswered: false, configLoadError: 'config store unavailable', onRetryConfigLoad });
    const row = screen.getByTestId('computer-use-config-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent(/config store unavailable/);
    fireEvent.click(screen.getByText('Retry'));
    expect(onRetryConfigLoad).toHaveBeenCalledTimes(1);
  });

  test('a refused write is surfaced verbatim', () => {
    renderPanel({
      configError:
        'Config update refused: computer_use_category_modes: close_or_quit actions always confirm individually',
    });
    expect(
      screen.getByText(
        'Config update refused: computer_use_category_modes: close_or_quit actions always confirm individually'
      )
    ).toBeInTheDocument();
  });

  describe('the acknowledgement gate', () => {
    test('the ack block names what control means and reports the ack', () => {
      const { props } = renderPanel({ computerUseEnabled: true });
      const block = screen.getByTestId('computer-use-ack-block');
      expect(block).toHaveTextContent(/Aether will click and type in your apps/);
      expect(block).toHaveTextContent(/ALWAYS ask for your confirmation first/);
      expect(block).toHaveTextContent(/stopping never kills the task itself/);
      fireEvent.click(screen.getByTestId('computer-use-ack-button'));
      expect(props.onComputerUseControlAckChange).toHaveBeenCalledWith(true);
    });

    test('the ack button waits for the master switch', () => {
      renderPanel({ computerUseEnabled: false });
      expect(screen.getByTestId('computer-use-ack-button')).toBeDisabled();
      expect(screen.getByText(/Turn on the master switch first/)).toBeInTheDocument();
    });

    test('Control cannot be enabled before the acknowledgement', () => {
      renderPanel({ computerUseEnabled: true, computerUseControlAck: false });
      const control = screen.getByTestId('computer-use-control-toggle');
      expect(control).toBeDisabled();
      expect(screen.getByText(/Needs the one-time acknowledgement above first/)).toBeInTheDocument();
    });

    test('once acknowledged, the ack block is gone and Control reports', () => {
      const { props } = renderPanel({
        computerUseEnabled: true,
        computerUseControlAck: true,
      });
      expect(screen.queryByTestId('computer-use-ack-block')).not.toBeInTheDocument();
      const control = screen.getByTestId('computer-use-control-toggle');
      expect(control).not.toBeDisabled();
      fireEvent.click(control);
      expect(props.onComputerUseControlChange).toHaveBeenCalledWith(true);
    });
  });

  describe('category modes', () => {
    test('ordinary categories report (category, mode) changes', () => {
      const { props } = renderPanel({ computerUseEnabled: true });
      fireEvent.change(screen.getByTestId('computer-use-category-interact'), {
        target: { value: 'allow_freely' },
      });
      expect(props.onComputerUseCategoryModeChange).toHaveBeenCalledWith(
        'interact',
        'allow_freely'
      );
    });

    test('hard-confirm categories are locked to Always confirm with the reason', () => {
      renderPanel({
        computerUseEnabled: true,
        // Even a stored looser value (hand-edited config) renders
        // locked: the select is disabled and shows always_confirm.
        computerUseCategoryModes: { close_or_quit: 'allow_freely' },
      });
      for (const id of ['close_or_quit', 'system']) {
        const select = screen.getByTestId(`computer-use-category-${id}`);
        expect(select).toBeDisabled();
        expect(select).toHaveValue('always_confirm');
      }
      expect(
        screen.getAllByText(/Locked to Always confirm: these never run under an envelope/)
      ).toHaveLength(2);
    });

    test('observe has no mode select — the Observe switch governs it', () => {
      renderPanel({ computerUseEnabled: true });
      expect(screen.queryByTestId('computer-use-category-observe')).not.toBeInTheDocument();
    });

    test('a stored mode renders in its select', () => {
      renderPanel({
        computerUseEnabled: true,
        computerUseCategoryModes: { clipboard: 'allow_with_envelope' },
      });
      expect(screen.getByTestId('computer-use-category-clipboard')).toHaveValue(
        'allow_with_envelope'
      );
    });
  });

  describe('pixel fallback and envelopes switches', () => {
    test('both report through their props and name their rules', () => {
      const { props } = renderPanel({ computerUseEnabled: true });
      fireEvent.click(screen.getByTestId('computer-use-pixel-fallback-toggle'));
      expect(props.onComputerUsePixelFallbackChange).toHaveBeenCalledWith(true);
      expect(screen.getByText(/always asks once per session/)).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('computer-use-envelopes-toggle'));
      expect(props.onComputerUseEnvelopesEnabledChange).toHaveBeenCalledWith(false);
      expect(
        screen.getByText(/Envelopes never cover Close or quit or System actions/)
      ).toBeInTheDocument();
    });

    test('both wait for the master switch', () => {
      renderPanel({ computerUseEnabled: false });
      expect(screen.getByTestId('computer-use-pixel-fallback-toggle')).toBeDisabled();
      expect(screen.getByTestId('computer-use-envelopes-toggle')).toBeDisabled();
    });
  });

  describe('per-app rules', () => {
    test('adding a rule reports the new list, trimmed and deduped', () => {
      const { props } = renderPanel({
        computerUseEnabled: true,
        computerUseAppRules: [{ app: 'WINWORD.EXE', verdict: 'allow' }],
      });
      fireEvent.change(screen.getByTestId('computer-use-app-rule-app'), {
        target: { value: '  winword.exe ' },
      });
      fireEvent.click(screen.getByTestId('computer-use-app-rule-add'));
      // Same app typed with different case/space replaces the old
      // rule instead of listing the app twice.
      expect(props.onComputerUseAppRulesChange).toHaveBeenCalledWith([
        { app: 'winword.exe', verdict: 'deny' },
      ]);
    });

    test('verdict flips and removals report the full new list', () => {
      const rules = [
        { app: 'A.EXE', verdict: 'deny' },
        { app: 'B.EXE', verdict: 'allow' },
      ];
      const { props } = renderPanel({
        computerUseEnabled: true,
        computerUseAppRules: rules,
      });
      fireEvent.change(screen.getByTestId('computer-use-app-rule-verdict-0'), {
        target: { value: 'allow' },
      });
      expect(props.onComputerUseAppRulesChange).toHaveBeenCalledWith([
        { app: 'A.EXE', verdict: 'allow' },
        { app: 'B.EXE', verdict: 'allow' },
      ]);
      fireEvent.click(screen.getByTestId('computer-use-app-rule-remove-1'));
      expect(props.onComputerUseAppRulesChange).toHaveBeenCalledWith([
        { app: 'A.EXE', verdict: 'deny' },
      ]);
    });
  });

  describe('remembered grants', () => {
    test('the panel asks for the grants list on mount', () => {
      const { sent } = renderPanel();
      expect(
        sent().some(
          (m) => m.type === 'DESKTOP_GRANTS_REQUEST' && m.payload.action === 'list'
        )
      ).toBe(true);
      expect(screen.getByText(/Loading remembered grants…/)).toBeInTheDocument();
    });

    test('an empty list says so', () => {
      const { simulateMessage } = renderPanel();
      act(() => {
        simulateMessage({
          type: 'DESKTOP_GRANTS_RESPONSE',
          payload: { success: true, grants: [] },
        });
      });
      expect(screen.getByText(/No remembered grants yet/)).toBeInTheDocument();
    });

    test('grants render and Revoke sends the revoke request', () => {
      const { simulateMessage, sent } = renderPanel();
      act(() => {
        simulateMessage({
          type: 'DESKTOP_GRANTS_RESPONSE',
          payload: {
            success: true,
            grants: [
              { id: 3, app: 'WINWORD.EXE', category: 'interact', granted_at: 1 },
            ],
          },
        });
      });
      const row = screen.getByTestId('computer-use-grant-3');
      expect(row).toHaveTextContent('WINWORD.EXE');
      expect(row).toHaveTextContent('interact');
      fireEvent.click(screen.getByTestId('computer-use-grant-revoke-3'));
      expect(
        sent().some(
          (m) =>
            m.type === 'DESKTOP_GRANTS_REQUEST' &&
            m.payload.action === 'revoke' &&
            m.payload.grant_id === 3
        )
      ).toBe(true);
    });

    test('a failed grants load is shown, not hidden', () => {
      const { simulateMessage } = renderPanel();
      act(() => {
        simulateMessage({
          type: 'DESKTOP_GRANTS_RESPONSE',
          payload: { success: false, error: 'grant store unavailable' },
        });
      });
      expect(screen.getByText('grant store unavailable')).toBeInTheDocument();
    });
  });
});
