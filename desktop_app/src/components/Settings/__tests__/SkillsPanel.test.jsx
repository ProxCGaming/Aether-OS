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
});
