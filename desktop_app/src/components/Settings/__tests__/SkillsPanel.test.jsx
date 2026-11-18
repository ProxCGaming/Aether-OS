/** Wave 5 (Annex C row 7): SkillsPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * skill list and the toggle send; these tests pin the load
 * state, the skill rows and the queued-toggle honesty. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import SkillsPanel from '../SkillsPanel';

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
});
