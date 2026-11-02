import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn()
  })
}));

function makeWsRef() {
  let messageHandler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
      removeEventListener: jest.fn(),
    },
  };
  return { wsRef, getHandler: () => messageHandler };
}

function feedTools(getHandler, tools) {
  act(() => {
    getHandler()({ data: JSON.stringify({ type: 'TOOL_LIST_RESPONSE', payload: { tools } }) });
  });
}

describe('SettingsPanel Tools Tab', () => {
  it('renders risk badges and disabled Always Allow notice for risky tools', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [
      {
        function: { name: 'execute_shell', description: 'Run a local shell command.' },
        risk: 'execute',
        always_allow_permitted: false,
        effective_policy: 'ask',
        policy_is_default: true,
        policy: 'ask',
      },
      {
        function: { name: 'read_file', description: 'Read a file.' },
        risk: 'read_only',
        always_allow_permitted: true,
        effective_policy: 'allow',
        policy_is_default: true,
        policy: 'allow',
      },
    ]);

    // Risky tool gets the Risky badge; the read-only tool gets none.
    expect(screen.getByText('Risky')).toBeInTheDocument();
    expect(screen.getByText('Always Allow is disabled for risky tools.')).toBeInTheDocument();
    expect(screen.queryByText('Write')).not.toBeInTheDocument();
    // Both tools are listed by name.
    expect(screen.getByText('execute_shell')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });

  it('shows an empty state when no tools are registered', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, []);

    expect(screen.getByText('No tools registered.')).toBeInTheDocument();
  });

  it('renders the Write badge for write-risk tools', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [
      {
        function: { name: 'write_file', description: 'Write a file.' },
        risk: 'write',
        always_allow_permitted: false,
        effective_policy: 'ask',
        policy_is_default: true,
        policy: 'ask',
      },
    ]);

    expect(screen.getByText('Write')).toBeInTheDocument();
  });
});

describe('SettingsPanel auxiliary models (Phase 9 UI honesty)', () => {
  it('renders only tasks with a real engine seam (Vision), no placebo dropdowns', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Models & Tasks'));
    expect(screen.getByText('⚙ Auxiliary models')).toBeInTheDocument();
    expect(screen.getByText('Vision')).toBeInTheDocument();

    // The six helper tasks with no engine call site must not be rendered.
    for (const label of ['Web extract', 'Compression', 'Skills hub', 'Approval', 'MCP', 'Title gen']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it('Reset all to main clears the vision override over the socket', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Models & Tasks'));
    fireEvent.click(screen.getByText('Reset all to main'));

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const reset = sent.find(m => m.type === 'AUXILIARY_MODEL_SET_REQUEST');
    expect(reset).toBeDefined();
    expect(reset.payload.task).toBe('vision');
    expect(reset.payload.model).toBe('auto');
  });
});

// Phase 10.3 C2: the theme buttons are gone. They only set `data-theme` on
// <html> — no CSS rule ever read it, nothing persisted the choice, and the
// app's colours are hardcoded dark inline styles, so no theme ever changed.
describe('SettingsPanel theme controls removed (Phase 10.3 C2)', () => {
  it('has no General menu and no theme buttons', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    expect(screen.queryByText('General')).not.toBeInTheDocument();
    expect(screen.queryByText('Appearance')).not.toBeInTheDocument();
    for (const label of ['Default', 'System Default', 'Light', 'Dark']) {
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument();
    }
  });

  it('no theme code remains in the SettingsPanel source', () => {
    // The component must not set data-theme anywhere (comments aside, the
    // setter call itself must be gone).
    const fs = require('fs');
    const path = require('path');
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'SettingsPanel.jsx'), 'utf8');
    expect(source).not.toContain("setAttribute('data-theme'");
    expect(source).not.toContain('setAttribute("data-theme"');
  });
});

// Phase 10.3 C2: the tool-policy dropdown used to offer a "default" option
// it refused to send (the handler returned early on it, and no engine
// message can clear a stored override). The option is gone; a tool at its
// default now shows the effective policy the engine resolves, with a note
// that no override is set — and every remaining option sends for real.
describe('SettingsPanel tool policy dropdown (Phase 10.3 C2)', () => {
  const readOnlyToolAtDefault = {
    function: { name: 'read_file', description: 'Read a file.' },
    risk: 'read_only',
    always_allow_permitted: true,
    effective_policy: 'allow',
    policy_is_default: true,
    policy: 'Require Approval', // what the engine sends as `policy` at default
  };

  it('shows the effective policy for a tool at default, with a no-override note', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [readOnlyToolAtDefault]);

    // The dropdown displays the effective policy (Always Allow), not a
    // placeholder, and says plainly that this is the default.
    expect(screen.getByRole('button', { name: 'Always Allow' })).toBeInTheDocument();
    expect(screen.getByText('Default policy — no override set. Pick one to override it.')).toBeInTheDocument();
  });

  it('offers only the three sendable policies — no default option — and sends the pick', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [readOnlyToolAtDefault]);

    // Open the dropdown: the menu must list exactly the real policies.
    fireEvent.click(screen.getByRole('button', { name: 'Always Allow' }));
    expect(screen.queryByText(/\(default\)/)).not.toBeInTheDocument();
    expect(screen.getByText('Require Approval')).toBeInTheDocument();
    expect(screen.getByText('Deny')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Require Approval'));

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const set = sent.filter(m => m.type === 'TOOL_POLICY_SET_REQUEST');
    expect(set).toHaveLength(1);
    expect(set[0].payload).toEqual({ tool_name: 'read_file', policy: 'Require Approval' });
  });

  it('shows the stored policy (and no default note) for a tool with an explicit override', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [{
      function: { name: 'read_file', description: 'Read a file.' },
      risk: 'read_only',
      always_allow_permitted: true,
      effective_policy: 'deny',
      policy_is_default: false,
      policy: 'Deny',
    }]);

    expect(screen.getByRole('button', { name: 'Deny' })).toBeInTheDocument();
    expect(screen.queryByText(/no override set/)).not.toBeInTheDocument();
  });

  it('no un-sendable default option remains in the SettingsPanel source', () => {
    const fs = require('fs');
    const path = require('path');
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'SettingsPanel.jsx'), 'utf8');
    expect(source).not.toContain('default_unset');
  });

  it('switches to Workspace Indexing tab when clicked', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Workspace Indexing'));
    expect(screen.getByRole('heading', { level: 2, name: 'Workspace Indexing' })).toBeInTheDocument();
  });

  it('switches to Usage tab when clicked', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Usage'));
    expect(screen.getByRole('heading', { level: 2, name: 'Usage & Budget' })).toBeInTheDocument();
  });
});

describe('SettingsPanel Voice Tuning (E8-5.2)', () => {
  function makeWsRef() {
    let messageHandler = null;
    const wsRef = {
      current: {
        readyState: 1,
        send: jest.fn(),
        addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
        removeEventListener: jest.fn(),
      },
    };
    return { wsRef, getHandler: () => messageHandler };
  }

  it('switches to Voice Tuning tab and renders endpointing sliders', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Voice Tuning'));
    expect(screen.getByRole('heading', { level: 2, name: 'Voice Tuning' })).toBeInTheDocument();
    expect(screen.getByText('Endpointing Thresholds')).toBeInTheDocument();

    const vadSlider = screen.getByTestId('vad-threshold-slider');
    const silenceSlider = screen.getByTestId('silence-duration-slider');
    expect(vadSlider).toBeInTheDocument();
    expect(silenceSlider).toBeInTheDocument();
    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.50');
    expect(screen.getByTestId('silence-duration-value')).toHaveTextContent('700 ms');
  });

  it('moving the VAD threshold slider dispatches CONFIG_UPDATE_REQUEST', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Voice Tuning'));
    const vadSlider = screen.getByTestId('vad-threshold-slider');

    fireEvent.change(vadSlider, { target: { value: '0.35' } });
    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.35');

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const update = sent.find(m => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.vad_threshold !== undefined);
    expect(update).toBeDefined();
    expect(update.payload.config.vad_threshold).toBe(0.35);
  });

  it('moving the silence duration slider dispatches CONFIG_UPDATE_REQUEST', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Voice Tuning'));
    const silenceSlider = screen.getByTestId('silence-duration-slider');

    fireEvent.change(silenceSlider, { target: { value: '450' } });
    expect(screen.getByTestId('silence-duration-value')).toHaveTextContent('450 ms');

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const update = sent.find(m => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.silence_duration_ms !== undefined);
    expect(update).toBeDefined();
    expect(update.payload.config.silence_duration_ms).toBe(450);
  });

  it('populates thresholds from CONFIG_GET_RESPONSE', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Voice Tuning'));

    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'CONFIG_GET_RESPONSE',
          payload: {
            config: {
              vad_threshold: 0.40,
              silence_duration_ms: 550,
            },
          },
        }),
      });
    });

    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.40');
    expect(screen.getByTestId('silence-duration-value')).toHaveTextContent('550 ms');
  });
});
