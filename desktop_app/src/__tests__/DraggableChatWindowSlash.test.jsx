import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest (same as App.test.jsx).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

const COMMANDS = [
  { command: '/new', description: 'Start a new chat' },
  { command: '/skill', description: 'Invoke a skill', type: 'skill_invoke' },
  { command: '/models', description: 'List available models' },
];

function renderChat(props = {}) {
  return render(
    <DraggableChatWindow
      messages={[]}
      onSendMessage={jest.fn()}
      onStopTask={jest.fn()}
      status="Online"
      isThinking={false}
      onApproveTool={jest.fn()}
      onApprovePlugin={jest.fn()}
      providers={[]}
      onChangeModel={jest.fn()}
      onDetach={jest.fn()}
      isFloating={false}
      initialInput=""
      slashCommands={COMMANDS}
      isSidebarCollapsed={false}
      {...props}
    />
  );
}

function typeInto(input, value) {
  fireEvent.change(input, { target: { value } });
}

// Phase 10.3 D2: the slash popup is the only way to discover slash
// commands; it must open on '/', filter as the user types, let the
// keyboard pick a command, and never leak a command into a sent message
// by accident.
describe('DraggableChatWindow slash popup (Phase 10.3 D2)', () => {
  it('typing / opens the popup listing all commands', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    expect(screen.getByText('/new')).toBeInTheDocument();
    expect(screen.getByText('/skill')).toBeInTheDocument();
    expect(screen.getByText('/models')).toBeInTheDocument();
    expect(screen.getByText('Invoke a skill')).toBeInTheDocument();
  });

  it('typing /sk filters to matching commands only', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/sk');
    expect(screen.getByText('/skill')).toBeInTheDocument();
    expect(screen.queryByText('/new')).not.toBeInTheDocument();
    expect(screen.queryByText('/models')).not.toBeInTheDocument();
  });

  it('filtering also matches on the description', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/avail');
    expect(screen.getByText('/models')).toBeInTheDocument();
    expect(screen.queryByText('/new')).not.toBeInTheDocument();
  });

  it('typing a space after the command closes the popup', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/new');
    // The popup item is present (assert on its description: '/new' itself
    // would also match the textarea's value in jsdom).
    expect(screen.getByText('Start a new chat')).toBeInTheDocument();
    typeInto(input, '/new ');
    expect(screen.queryByText('Start a new chat')).not.toBeInTheDocument();
  });

  it('plain text without a leading slash never opens the popup', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, 'hello /new');
    expect(screen.queryByText('/models')).not.toBeInTheDocument();
  });

  it('Enter inserts the first (highlighted) command plus a space and closes the popup', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input.value).toBe('/new ');
    expect(screen.queryByText('/models')).not.toBeInTheDocument();
  });

  it('ArrowDown then Enter inserts the second command', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input.value).toBe('/skill ');
    expect(screen.queryByText('/models')).not.toBeInTheDocument();
  });

  it('ArrowUp wraps to the last command', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input.value).toBe('/models ');
  });

  it('Tab selects the highlighted command like Enter does', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/mo');
    fireEvent.keyDown(input, { key: 'Tab' });
    expect(input.value).toBe('/models ');
  });

  it('Escape closes the popup without changing the input', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/sk');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByText('/skill')).not.toBeInTheDocument();
    expect(input.value).toBe('/sk');
  });

  it('clicking a popup item inserts it and closes the popup', () => {
    renderChat();
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    fireEvent.click(screen.getByText('/models'));
    expect(input.value).toBe('/models ');
    expect(screen.queryByText('/skill')).not.toBeInTheDocument();
  });

  it('no popup appears when there are no slash commands at all', () => {
    renderChat({ slashCommands: [] });
    const input = screen.getByPlaceholderText('Message Aether...');
    typeInto(input, '/');
    expect(screen.queryByText('/new')).not.toBeInTheDocument();
  });
});
