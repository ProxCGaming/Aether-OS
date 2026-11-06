import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ChatComposer from '../ChatComposer';

// Wave 5 (Annex C row 6, composer seam): the composer extracted
// from DraggableChatWindow is presentational — these tests pin
// the props contract the window relies on: values render, and
// every interaction forwards to the handler prop unchanged.

function makeProps(overrides = {}) {
  return {
    input: '',
    attachments: [],
    isThinking: false,
    planMode: false,
    isDictating: false,
    dictationError: null,
    showSlashPopup: false,
    filteredCommands: [],
    slashIndex: 0,
    slashPopupRef: React.createRef(),
    textareaRef: React.createRef(),
    onSubmit: jest.fn((e) => e.preventDefault()),
    onInputChange: jest.fn(),
    onInputKeyDown: jest.fn(),
    onTogglePlanMode: jest.fn(),
    onTriggerSnip: jest.fn(),
    onRemoveAttachment: jest.fn(),
    onSelectSlashCommand: jest.fn(),
    onMicStart: jest.fn(),
    onMicEnd: jest.fn(),
    onMicKeyDown: jest.fn(),
    onMicKeyUp: jest.fn(),
    onStopTask: jest.fn(),
    onDismissDictationError: jest.fn(),
    ...overrides,
  };
}

describe('ChatComposer', () => {
  test('renders the textarea with the current input and forwards change/keydown', () => {
    const props = makeProps({ input: 'hello' });
    render(<ChatComposer {...props} />);
    const textarea = screen.getByPlaceholderText('Message Aether...');
    expect(textarea).toHaveValue('hello');
    fireEvent.change(textarea, { target: { value: 'hello!' } });
    expect(props.onInputChange).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(props.onInputKeyDown).toHaveBeenCalledTimes(1);
  });

  test('submitting the form forwards to onSubmit', () => {
    const props = makeProps({ input: 'send me' });
    const { container } = render(<ChatComposer {...props} />);
    fireEvent.submit(container.querySelector('form'));
    expect(props.onSubmit).toHaveBeenCalledTimes(1);
  });

  test('send button is disabled with no input and no attachments', () => {
    const props = makeProps();
    const { container } = render(<ChatComposer {...props} />);
    const sendBtn = container.querySelector('button[type="submit"]');
    expect(sendBtn).toBeDisabled();
  });

  test('while thinking, stop button forwards to onStopTask and steering submit appears with text', () => {
    const props = makeProps({ isThinking: true, input: 'steer this' });
    render(<ChatComposer {...props} />);
    fireEvent.click(screen.getByLabelText('Stop generation'));
    expect(props.onStopTask).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Queue steering message')).toBeInTheDocument();
  });

  test('slash popup lists commands, highlights slashIndex, click selects the command', () => {
    const commands = [
      { command: '/plan', description: 'Plan the task', type: 'builtin' },
      { command: '/research', description: 'Research a topic', type: 'skill_invoke' },
    ];
    const props = makeProps({ showSlashPopup: true, filteredCommands: commands, slashIndex: 1, input: '/r' });
    render(<ChatComposer {...props} />);
    expect(screen.getByText('/plan')).toBeInTheDocument();
    expect(screen.getByText('SKILL')).toBeInTheDocument();
    fireEvent.click(screen.getByText('/research'));
    expect(props.onSelectSlashCommand).toHaveBeenCalledWith(commands[1]);
  });

  test('slash popup stays hidden when there are no filtered commands', () => {
    const props = makeProps({ showSlashPopup: true, filteredCommands: [], input: '/' });
    render(<ChatComposer {...props} />);
    expect(screen.queryByText('/plan')).not.toBeInTheDocument();
  });

  test('attachment previews render and remove forwards the index', () => {
    const attachments = [
      { data: 'data:image/png;base64,AAA', name: 'snip one' },
      { path: '/tmp/two.png', name: 'snip two' },
    ];
    const props = makeProps({ attachments });
    render(<ChatComposer {...props} />);
    expect(screen.getByTestId('composer-attachments-preview')).toBeInTheDocument();
    expect(screen.getAllByTestId('composer-attachment-thumbnail')).toHaveLength(2);
    fireEvent.click(screen.getAllByTestId('remove-attachment-btn')[1]);
    expect(props.onRemoveAttachment).toHaveBeenCalledWith(1);
  });

  test('plan toggle reflects planMode and snip button forwards', () => {
    const props = makeProps({ planMode: true });
    render(<ChatComposer {...props} />);
    const planBtn = screen.getByLabelText('Toggle plan mode');
    expect(planBtn).toHaveAttribute('aria-pressed', 'true');
    expect(planBtn.className).toContain('active');
    fireEvent.click(planBtn);
    expect(props.onTogglePlanMode).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('snip-ask-btn'));
    expect(props.onTriggerSnip).toHaveBeenCalledTimes(1);
  });

  test('mic button forwards press/release and shows recording state', () => {
    const props = makeProps({ isDictating: true });
    render(<ChatComposer {...props} />);
    const mic = screen.getByTestId('composer-mic-btn');
    expect(mic.className).toContain('recording');
    expect(mic).toHaveAttribute('aria-pressed', 'true');
    fireEvent.mouseDown(mic);
    expect(props.onMicStart).toHaveBeenCalledTimes(1);
    fireEvent.mouseUp(mic);
    expect(props.onMicEnd).toHaveBeenCalledTimes(1);
  });

  test('dictation error renders and its dismiss forwards', () => {
    const props = makeProps({ dictationError: 'Microphone unavailable: denied' });
    render(<ChatComposer {...props} />);
    expect(screen.getByTestId('dictation-error')).toHaveTextContent('Microphone unavailable: denied');
    fireEvent.click(screen.getByTestId('dictation-error').querySelector('button'));
    expect(props.onDismissDictationError).toHaveBeenCalledTimes(1);
  });
});
