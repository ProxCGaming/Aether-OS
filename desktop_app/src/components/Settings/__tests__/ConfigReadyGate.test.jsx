/** RUX-C-11 (Wave 3 slice 2): config-controlled panels must not
 * present factory defaults as the user's saved configuration.
 * Slice 1 added the explanatory banners; this slice disables the
 * controls themselves until the first config answer lands. */
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrchestrationPanel from '../OrchestrationPanel';
import SpeechSettingsPanel from '../SpeechSettingsPanel';
import ProjectConfigPanel from '../ProjectConfigPanel';

describe('OrchestrationPanel config gate (RUX-C-11)', () => {
  it('configReady=false disables the whole control set', () => {
    render(<OrchestrationPanel config={null} configError="" onConfigUpdate={jest.fn()} configReady={false} />);
    expect(screen.getByTestId('orchestration-config-gate')).toBeDisabled();
    expect(screen.getByTestId('spawn-restrict-toggle')).toBeDisabled();
    expect(screen.getByTestId('compaction-save')).toBeDisabled();
  });

  it('configReady=true leaves the controls live', () => {
    render(<OrchestrationPanel config={{}} configError="" onConfigUpdate={jest.fn()} configReady />);
    expect(screen.getByTestId('orchestration-config-gate')).not.toBeDisabled();
    expect(screen.getByTestId('spawn-restrict-toggle')).not.toBeDisabled();
  });

  it('without the prop, readiness derives from the config itself', () => {
    const { rerender } = render(
      <OrchestrationPanel config={null} configError="" onConfigUpdate={jest.fn()} />,
    );
    expect(screen.getByTestId('orchestration-config-gate')).toBeDisabled();
    rerender(<OrchestrationPanel config={{ supervisor_delegation_cap: 3 }} configError="" onConfigUpdate={jest.fn()} />);
    expect(screen.getByTestId('orchestration-config-gate')).not.toBeDisabled();
  });
});

describe('SpeechSettingsPanel config gate (RUX-C-11)', () => {
  it('configReady=false disables the selects (the defaults are not the user\'s values)', () => {
    render(
      <SpeechSettingsPanel
        config={null}
        configError=""
        onConfigUpdate={jest.fn()}
        configReady={false}
        wsRef={{ current: null }}
      />,
    );
    expect(screen.getByTestId('speech-config-gate')).toBeDisabled();
    const sttButton = screen.getByTestId('speech-stt-select').querySelector('button');
    expect(sttButton).toBeDisabled();
  });

  it('a landed config leaves the selects live', () => {
    render(
      <SpeechSettingsPanel
        config={{ speech_stt_model: 'base' }}
        configError=""
        onConfigUpdate={jest.fn()}
        wsRef={{ current: null }}
      />,
    );
    expect(screen.getByTestId('speech-config-gate')).not.toBeDisabled();
    const sttButton = screen.getByTestId('speech-stt-select').querySelector('button');
    expect(sttButton).not.toBeDisabled();
  });
});

describe('ProjectConfigPanel config gate (RUX-C-11)', () => {
  it('pre-answer: loading root line, editor and save disabled, no false "No active workspace."', () => {
    render(
      <ProjectConfigPanel
        workspaceRoot=""
        content=""
        exists={false}
        forbiddenKeys={[]}
        warning=""
        status=""
        onSave={jest.fn()}
        configReady={false}
      />,
    );
    expect(screen.getByTestId('project-config-root')).toHaveTextContent('Loading the project layer…');
    expect(screen.queryByText('No active workspace.')).not.toBeInTheDocument();
    expect(screen.getByTestId('project-config-content')).toBeDisabled();
    expect(screen.getByTestId('project-config-save')).toBeDisabled();
  });

  it('answered with no workspace: the genuine "No active workspace." renders', () => {
    render(
      <ProjectConfigPanel
        workspaceRoot=""
        content=""
        exists={false}
        forbiddenKeys={[]}
        warning=""
        status=""
        onSave={jest.fn()}
        configReady
      />,
    );
    expect(screen.getByTestId('project-config-root')).toHaveTextContent('No active workspace.');
  });
});
