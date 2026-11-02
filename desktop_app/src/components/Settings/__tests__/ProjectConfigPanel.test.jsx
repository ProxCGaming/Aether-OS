import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProjectConfigPanel from '../ProjectConfigPanel';

// ProjectConfigPanel (E6-F05) is controlled: SettingsPanel owns the
// socket, fetches the project layer, and passes the applied layer
// down as props; the panel shows the layer itself and reports saves
// through onSave with the raw text. These tests pin the per-panel
// contract the commit gate requires (a <Name>.test.jsx per
// component); the shared HandedOffSurfaces suite covers the
// cross-panel story.

const baseProps = {
  workspaceRoot: '/home/u/project',
  content: '{\n  "reasoning_effort": "low"\n}',
  exists: true,
  forbiddenKeys: ['path_rules'],
  warning: '',
  status: '',
};

describe('ProjectConfigPanel', () => {
  test('shows the workspace, the layer text itself, and the global-only keys', () => {
    render(<ProjectConfigPanel {...baseProps} onSave={jest.fn()} />);
    expect(screen.getByTestId('project-config-root')).toHaveTextContent(
      'Workspace: /home/u/project');
    expect(screen.getByTestId('project-config-content')).toHaveValue(
      '{\n  "reasoning_effort": "low"\n}');
    expect(screen.getByTestId('project-config-forbidden')).toHaveTextContent(/path_rules/);
  });

  test('saving without edits reports the applied content verbatim', () => {
    const onSave = jest.fn();
    render(<ProjectConfigPanel {...baseProps} onSave={onSave} />);
    fireEvent.click(screen.getByTestId('project-config-save'));
    expect(onSave).toHaveBeenCalledWith('{\n  "reasoning_effort": "low"\n}');
  });

  test('without a workspace the editor is disabled and says so', () => {
    render(
      <ProjectConfigPanel {...baseProps} workspaceRoot="" content="" onSave={jest.fn()} />
    );
    expect(screen.getByTestId('project-config-root')).toHaveTextContent('No active workspace.');
    expect(screen.getByTestId('project-config-content')).toBeDisabled();
    expect(screen.getByTestId('project-config-save')).toBeDisabled();
  });

  test('a layer that does not exist yet shows the starter placeholder', () => {
    render(<ProjectConfigPanel {...baseProps} content="" exists={false} onSave={jest.fn()} />);
    expect(screen.getByTestId('project-config-content').getAttribute('placeholder'))
      .toContain('"reasoning_effort": "high"');
  });

  test('a freshly applied layer re-syncs the editor', () => {
    const { rerender } = render(<ProjectConfigPanel {...baseProps} onSave={jest.fn()} />);
    rerender(
      <ProjectConfigPanel
        {...baseProps}
        content={'{\n  "reasoning_effort": "high"\n}'}
        onSave={jest.fn()}
      />
    );
    expect(screen.getByTestId('project-config-content')).toHaveValue(
      '{\n  "reasoning_effort": "high"\n}');
  });

  test('no forbidden-keys note renders when the list is empty', () => {
    render(<ProjectConfigPanel {...baseProps} forbiddenKeys={[]} onSave={jest.fn()} />);
    expect(screen.queryByTestId('project-config-forbidden')).not.toBeInTheDocument();
  });

  test('a status passed down is shown next to the save button', () => {
    render(<ProjectConfigPanel {...baseProps} status="Saved." onSave={jest.fn()} />);
    expect(screen.getByTestId('project-config-status')).toHaveTextContent('Saved.');
  });
});
