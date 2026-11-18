/** Wave 5 (Annex C row 7): SkillsPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * skill list and the toggle send; these tests pin the load
 * state, the skill rows and the queued-toggle honesty. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SkillsPanel from '../SkillsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

// Wave 5 (D4-6): the panel now owns its install/search socket
// traffic (the list and toggle stay shell-owned props), so the
// socket context is mocked the PluginsPanel way.
jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeSocketHarness() {
  let messageListeners = [];
  const mockWs = {
    addEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners.push(cb);
    }),
    removeEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
    }),
  };
  const sendMessage = jest.fn();
  useAetherSocket.mockReturnValue({
    wsRef: { current: mockWs },
    sendMessage,
    isConnected: true,
  });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { simulateMessage, sendMessage };
}

beforeEach(() => {
  makeSocketHarness();
});

function baseProps(overrides = {}) {
  return {
    skills: [],
    skillsAnswered: true,
    isConnected: true,
    pendingSkillToggleFor: jest.fn(() => undefined),
    onSkillToggle: jest.fn(),
    ...overrides,
  };
}

const SKILL = {
  name: 'code-review',
  source: 'workspace',
  description: 'Reviews code changes',
  slash_commands: ['/review'],
  required_tools: ['read_file'],
  enabled: true,
};

describe('SkillsPanel', () => {
  test('before the answer lands the load state shows, not the empty text (RUX-C-01)', () => {
    render(<SkillsPanel {...baseProps({ skillsAnswered: false })} />);
    expect(screen.getByTestId('skills-load-state')).toBeInTheDocument();
    expect(screen.queryByText(/No skills found in workspace/)).not.toBeInTheDocument();
  });

  test('answered-empty renders the genuine empty state', () => {
    render(<SkillsPanel {...baseProps()} />);
    expect(screen.getByText(/No skills found in workspace/)).toBeInTheDocument();
  });

  test('a skill renders with its slash command and the toggle fires the handler', () => {
    const props = baseProps({ skills: [SKILL] });
    render(<SkillsPanel {...props} />);
    expect(screen.getByText('code-review')).toBeInTheDocument();
    expect(screen.getByText('/review')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Disable skill'));
    expect(props.onSkillToggle).toHaveBeenCalledWith(SKILL);
  });

  test('a queued toggle disables the button and names the pending change (RUX-A-14)', () => {
    render(<SkillsPanel {...baseProps({
      skills: [SKILL],
      pendingSkillToggleFor: jest.fn(() => ({ payload: { name: 'code-review', enabled: false } })),
    })} />);
    expect(screen.getByText('Disable skill')).toBeDisabled();
    expect(screen.getByText(/Disable queued — will apply when the engine reconnects/)).toBeInTheDocument();
  });

  test('a load_error skill renders as an Invalid row with its reason (D4-5 / FF4-07)', () => {
    const BROKEN = {
      name: 'broken-skill',
      source: 'workspace',
      description: '',
      slash_commands: [],
      required_tools: [],
      enabled: false,
      state: 'load_error',
      error: 'No YAML frontmatter found in /ws/.agents/skills/broken-skill/SKILL.md',
    };
    render(<SkillsPanel {...baseProps({ skills: [BROKEN] })} />);
    expect(screen.getByText('broken-skill')).toBeInTheDocument();
    expect(screen.getByText('Invalid')).toBeInTheDocument();
    expect(screen.getByText(/No YAML frontmatter found/)).toBeInTheDocument();
    // Nothing to toggle: it never loaded.
    expect(screen.queryByText('Enable skill')).not.toBeInTheDocument();
    expect(screen.queryByText('Disable skill')).not.toBeInTheDocument();
    // And it is a row, not an absence: the empty state stays away.
    expect(screen.queryByText(/No skills found in workspace/)).not.toBeInTheDocument();
  });

  test('valid and invalid skills render side by side (D4-5)', () => {
    const BROKEN = {
      name: 'broken-skill',
      source: 'global',
      description: '',
      slash_commands: [],
      required_tools: [],
      enabled: false,
      state: 'load_error',
      error: 'Expecting value: line 1 column 1 (char 0)',
    };
    render(<SkillsPanel {...baseProps({ skills: [SKILL, BROKEN] })} />);
    expect(screen.getByText('code-review')).toBeInTheDocument();
    expect(screen.getByText('Disable skill')).toBeInTheDocument();
    expect(screen.getByText('broken-skill')).toBeInTheDocument();
    expect(screen.getByText('Invalid')).toBeInTheDocument();
  });

  describe('shared capability search/filter (D4-1)', () => {
    const DISABLED = { ...SKILL, name: 'muted-skill', enabled: false, slash_commands: [], required_tools: [] };
    const BROKEN = {
      name: 'broken-skill', source: 'global', description: '', slash_commands: [],
      required_tools: [], enabled: false, state: 'load_error', error: 'bad manifest',
    };
    const ALL = [SKILL, DISABLED, BROKEN];

    test('a name fragment narrows the list', () => {
      render(<SkillsPanel {...baseProps({ skills: ALL })} />);
      fireEvent.change(screen.getByPlaceholderText('Search skills by name…'), { target: { value: 'muted' } });
      expect(screen.getByText('muted-skill')).toBeInTheDocument();
      expect(screen.queryByText('code-review')).not.toBeInTheDocument();
      expect(screen.queryByText('broken-skill')).not.toBeInTheDocument();
      expect(screen.getByText('Showing 1 of 3')).toBeInTheDocument();
    });

    test('the Disabled chip shows only disabled skills; the Error chip only the invalid row', () => {
      render(<SkillsPanel {...baseProps({ skills: ALL })} />);
      fireEvent.click(screen.getByText('Disabled · 1'));
      expect(screen.getByText('muted-skill')).toBeInTheDocument();
      expect(screen.queryByText('code-review')).not.toBeInTheDocument();
      // A load_error skill is an error, not a disabled skill.
      expect(screen.queryByText('broken-skill')).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('Error · 1'));
      expect(screen.getByText('broken-skill')).toBeInTheDocument();
      expect(screen.queryByText('muted-skill')).not.toBeInTheDocument();
    });

    test('the Updates chip honestly matches nothing yet and offers the clear action', () => {
      render(<SkillsPanel {...baseProps({ skills: ALL })} />);
      fireEvent.click(screen.getByText('Updates · 0'));
      expect(screen.getByTestId('skills-no-results')).toBeInTheDocument();
      expect(screen.getByText('No matching skills')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Show all skills'));
      expect(screen.getByText('code-review')).toBeInTheDocument();
      expect(screen.getByText('muted-skill')).toBeInTheDocument();
      expect(screen.getByText('broken-skill')).toBeInTheDocument();
    });
  });

  describe('pin state on rows (D4-6 / FF4-04 skills half)', () => {
    const PINNED = {
      ...SKILL,
      pin: { status: 'installed', source_type: 'local', source: '/home/user/catalogue', ref: '1.0', hash: 'a'.repeat(64) },
    };
    const DRIFTED = {
      ...SKILL,
      name: 'edited-skill',
      pin: { status: 'drifted', source_type: 'local', source: '/home/user/catalogue', ref: '1.0', hash: 'b'.repeat(64) },
    };

    test('a pinned skill shows its source and pin prefix', () => {
      render(<SkillsPanel {...baseProps({ skills: [PINNED] })} />);
      expect(screen.getByText(/Installed from \/home\/user\/catalogue · pin aaaaaaaaaaaa…/)).toBeInTheDocument();
      expect(screen.getByText(/match the recorded pin/)).toBeInTheDocument();
      expect(screen.queryByText('Modified vs pin')).not.toBeInTheDocument();
    });

    test('a drifted skill shows Modified vs pin and lands under the Updates chip', () => {
      render(<SkillsPanel {...baseProps({ skills: [DRIFTED] })} />);
      expect(screen.getByText('Modified vs pin')).toBeInTheDocument();
      expect(screen.getByText(/no longer match the recorded pin/)).toBeInTheDocument();
      expect(screen.getByText('Updates · 1')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Updates · 1'));
      expect(screen.getByText('edited-skill')).toBeInTheDocument();
    });
  });

  describe('install/search flow (D4-6)', () => {
    const PIN = {
      name: 'hello-skill',
      description: 'Says hello',
      version: '1.0',
      source_type: 'local',
      source: '/home/user/catalogue',
      hash: 'c'.repeat(64),
    };

    function searchAndReply(simulateMessage, sendMessage) {
      fireEvent.change(screen.getByPlaceholderText('Path to a folder of skills'), { target: { value: '/home/user/catalogue' } });
      fireEvent.click(screen.getByText('Search'));
      const call = sendMessage.mock.calls.find(([type]) => type === 'SKILL_SEARCH_REQUEST');
      expect(call).toBeTruthy();
      expect(call[1].payload).toEqual({ source_type: 'local', locator: '/home/user/catalogue', query: '' });
      return call[1].request_id;
    }

    test('search → prepare → pin card → install lands disabled and refreshes the list', () => {
      const { simulateMessage, sendMessage } = makeSocketHarness();
      render(<SkillsPanel {...baseProps()} />);
      const searchId = searchAndReply(simulateMessage, sendMessage);
      simulateMessage({
        type: 'SKILL_SEARCH_RESPONSE',
        request_id: searchId,
        payload: { success: true, skills: [{ name: 'hello-skill', description: 'Says hello', version: '1.0' }] },
      });
      fireEvent.click(screen.getByText('Install…'));
      const prepareCall = sendMessage.mock.calls.find(([type]) => type === 'SKILL_INSTALL_PREPARE_REQUEST');
      expect(prepareCall[1].payload).toEqual({ source_type: 'local', locator: '/home/user/catalogue', name: 'hello-skill' });
      simulateMessage({
        type: 'SKILL_INSTALL_PREPARE_RESPONSE',
        request_id: prepareCall[1].request_id,
        payload: { success: true, pin: PIN },
      });
      // The trust card shows the pin before anything is written.
      expect(screen.getByTestId('skill-install-card')).toBeInTheDocument();
      expect(screen.getByText('c'.repeat(64))).toBeInTheDocument();
      expect(screen.getByText(/It will be installed disabled/)).toBeInTheDocument();
      fireEvent.click(screen.getByText('Install'));
      const installCall = sendMessage.mock.calls.find(([type]) => type === 'SKILL_INSTALL_REQUEST');
      expect(installCall[1].payload).toEqual({
        source_type: 'local', locator: '/home/user/catalogue',
        name: 'hello-skill', expected_hash: 'c'.repeat(64),
      });
      simulateMessage({
        type: 'SKILL_INSTALL_RESPONSE',
        request_id: installCall[1].request_id,
        payload: { success: true, name: 'hello-skill', action: 'installed', skills: [] },
      });
      expect(screen.getByTestId('skill-install-notice')).toHaveTextContent(/disabled/);
      expect(sendMessage).toHaveBeenCalledWith('SKILL_LIST_REQUEST', expect.objectContaining({ payload: {} }));
      expect(screen.queryByTestId('skill-install-card')).not.toBeInTheDocument();
    });

    test('a source the engine cannot serve is answered honestly, verbatim', () => {
      const { simulateMessage, sendMessage } = makeSocketHarness();
      render(<SkillsPanel {...baseProps()} />);
      fireEvent.change(screen.getByLabelText('Skill source type'), { target: { value: 'github' } });
      fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'octo/skills' } });
      fireEvent.click(screen.getByText('Search'));
      const call = sendMessage.mock.calls.find(([type]) => type === 'SKILL_SEARCH_REQUEST');
      simulateMessage({
        type: 'SKILL_SEARCH_RESPONSE',
        request_id: call[1].request_id,
        payload: {
          success: false,
          error: "the 'github' skill source has no remote registry access in this build — skills can be installed from a local directory",
        },
      });
      expect(screen.getByTestId('skill-search-error')).toHaveTextContent(/no remote registry access in this build/);
    });

    test('an install refused after review shows the engine reason and keeps the card open', () => {
      const { simulateMessage, sendMessage } = makeSocketHarness();
      render(<SkillsPanel {...baseProps()} />);
      const searchId = searchAndReply(simulateMessage, sendMessage);
      simulateMessage({
        type: 'SKILL_SEARCH_RESPONSE',
        request_id: searchId,
        payload: { success: true, skills: [{ name: 'hello-skill', description: 'Says hello', version: '1.0' }] },
      });
      fireEvent.click(screen.getByText('Install…'));
      const prepareCall = sendMessage.mock.calls.find(([type]) => type === 'SKILL_INSTALL_PREPARE_REQUEST');
      simulateMessage({
        type: 'SKILL_INSTALL_PREPARE_RESPONSE',
        request_id: prepareCall[1].request_id,
        payload: { success: true, pin: PIN },
      });
      fireEvent.click(screen.getByText('Install'));
      const installCall = sendMessage.mock.calls.find(([type]) => type === 'SKILL_INSTALL_REQUEST');
      simulateMessage({
        type: 'SKILL_INSTALL_RESPONSE',
        request_id: installCall[1].request_id,
        payload: { success: false, name: 'hello-skill', error: "skill 'hello-skill' changed since it was reviewed — search and review it again before installing" },
      });
      expect(screen.getByTestId('skill-install-error')).toHaveTextContent(/changed since it was reviewed/);
      expect(screen.getByTestId('skill-install-card')).toBeInTheDocument();
    });
  });
});
