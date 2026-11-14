/** Master build S11: the Conversation settings surface.
 * The EQ switch is controlled (SettingsPanel owns the config
 * and the one CONFIG_UPDATE flow); this panel renders the
 * applied value, reports edits, shows a load state instead of
 * a guessed value before the config answer, and surfaces a
 * refused write verbatim. The copy carries the honest contract:
 * local signals, nothing about mood stored, no feelings claim. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ConversationPanel from '../ConversationPanel';

function renderPanel(props = {}) {
  return render(
    <ConversationPanel
      configAnswered
      isConnected
      eqEnabled
      onEqEnabledChange={jest.fn()}
      {...props}
    />,
  );
}

describe('ConversationPanel (S11)', () => {
  test('renders the EQ switch on, with the honest contract in the copy', () => {
    renderPanel();
    const toggle = screen.getByTestId('eq-enabled-toggle');
    expect(toggle).toBeChecked();
    // What it does, what it does not do — both halves stated.
    expect(screen.getByText(/nothing about your mood is stored in long-term memory/)).toBeInTheDocument();
    expect(screen.getByText(/never claims to know how you feel/)).toBeInTheDocument();
    expect(screen.getByText(/read on this machine from the current conversation only/)).toBeInTheDocument();
  });

  test('renders the applied off state', () => {
    renderPanel({ eqEnabled: false });
    expect(screen.getByTestId('eq-enabled-toggle')).not.toBeChecked();
  });

  test('toggling reports the edit through the controlled prop (the CONFIG_UPDATE path)', () => {
    const onEqEnabledChange = jest.fn();
    renderPanel({ onEqEnabledChange });
    fireEvent.click(screen.getByTestId('eq-enabled-toggle'));
    expect(onEqEnabledChange).toHaveBeenCalledWith(false);
  });

  test('before the config answer, a load state shows instead of a guessed switch', () => {
    renderPanel({ configAnswered: false });
    expect(screen.getByTestId('conversation-config-load-state')).toBeInTheDocument();
    expect(screen.queryByTestId('eq-enabled-toggle')).not.toBeInTheDocument();
  });

  test('a refused write is surfaced verbatim', () => {
    renderPanel({ configError: 'eq_enabled: must be true or false' });
    expect(screen.getByText('eq_enabled: must be true or false')).toBeInTheDocument();
  });
});
