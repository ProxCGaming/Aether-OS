import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import PathRulesPanel from '../PathRulesPanel';

// PathRulesPanel (E6-F05) is controlled: SettingsPanel owns the
// socket and the applied config; the panel edits the stored rules
// locally and reports the cleaned list through onConfigUpdate as
// { path_rules: [...] }. These tests pin the per-panel contract the
// commit gate requires (a <Name>.test.jsx per component); the shared
// HandedOffSurfaces suite covers the cross-panel story.

function renderPanel(rules, configError = '') {
  const onConfigUpdate = jest.fn();
  render(<PathRulesPanel rules={rules} configError={configError} onConfigUpdate={onConfigUpdate} />);
  return { onConfigUpdate };
}

describe('PathRulesPanel', () => {
  test('an empty rules list renders the empty-state copy and no rows', () => {
    renderPanel([]);
    expect(screen.getByText('No path rules. Tool policies apply as-is.')).toBeInTheDocument();
    expect(screen.queryByTestId('path-rule-prefix-0')).not.toBeInTheDocument();
  });

  test('a stored rule renders its prefix, verdict and comma-joined scopes', () => {
    renderPanel([
      { prefix: '/srv/data', verdict: 'Require Approval', tools: ['read_file', 'list_dir'], risks: ['read_only', 'write'] },
    ]);
    expect(screen.getByTestId('path-rule-prefix-0')).toHaveValue('/srv/data');
    expect(screen.getByTestId('path-rule-verdict-0')).toHaveValue('Require Approval');
    expect(screen.getByTestId('path-rule-tools-0')).toHaveValue('read_file, list_dir');
    expect(screen.getByTestId('path-rule-risks-0')).toHaveValue('read_only, write');
  });

  test('a stored rule with an unknown verdict falls back to Require Approval', () => {
    renderPanel([{ prefix: '/x', verdict: 'Sometimes Allow' }]);
    expect(screen.getByTestId('path-rule-verdict-0')).toHaveValue('Require Approval');
  });

  test('save trims the prefix, splits the scope lists, and confirms', () => {
    const { onConfigUpdate } = renderPanel([]);
    fireEvent.click(screen.getByTestId('path-rule-add'));
    fireEvent.change(screen.getByTestId('path-rule-prefix-0'), {
      target: { value: '  ~/projects  ' },
    });
    fireEvent.change(screen.getByTestId('path-rule-verdict-0'), {
      target: { value: 'Always Allow' },
    });
    fireEvent.change(screen.getByTestId('path-rule-tools-0'), {
      target: { value: ' write_file , read_file ' },
    });
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      path_rules: [
        { prefix: '~/projects', verdict: 'Always Allow', tools: ['write_file', 'read_file'] },
      ],
    });
    expect(screen.getByText('Saved.')).toBeInTheDocument();
  });

  test('an unknown risk is refused locally, naming the risk, and sends nothing', () => {
    const { onConfigUpdate } = renderPanel([{ prefix: '/x', verdict: 'Deny' }]);
    fireEvent.change(screen.getByTestId('path-rule-risks-0'), {
      target: { value: 'read_only, explosive' },
    });
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(screen.getByTestId('path-rules-error')).toHaveTextContent(/unknown risk "explosive"/);
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test('removing the only row and saving reports an empty list', () => {
    const { onConfigUpdate } = renderPanel([{ prefix: '/x', verdict: 'Deny' }]);
    fireEvent.click(screen.getByTestId('path-rule-remove-0'));
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ path_rules: [] });
  });

  test('an engine refusal passed down as configError is shown', () => {
    renderPanel([], 'path_rules: must be a list');
    expect(screen.getByTestId('path-rules-error')).toHaveTextContent('path_rules: must be a list');
  });
});
