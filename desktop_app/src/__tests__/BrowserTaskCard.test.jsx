import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { BrowserTaskCard } from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest (same as the sibling
// DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; the chat window calls it on new messages.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

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
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...props}
    />
  );
}

const browserMessages = [
  { id: 'u1', role: 'user', content: 'book a table for two', isFinal: true, thoughts: [] },
  {
    id: 'a1',
    role: 'agent',
    content: 'On it.',
    isFinal: true,
    thoughts: [
      {
        type: 'TOOL_ACTIVITY',
        payload: {
          tool_name: 'browser_navigate',
          tool_args: { url: 'https://example.com/' },
          status: 'completed',
        },
      },
    ],
  },
];

// Epic 8, E8-6.1: when a task drives the agent browser, its chat shows
// a browser task card with pause / take-control / stop. The card talks
// back through onBrowserControl(action); App owns the engine call
// (BROWSER_TASK_PAUSE_REQUEST). A CAPTCHA/2FA park shows the
// "waiting for you" state — the agent never solves challenges itself.
describe('BrowserTaskCard (E8-6.1)', () => {
  test('renders the standalone card with all three controls', () => {
    const onControl = jest.fn();
    render(<BrowserTaskCard session={{ controlState: 'agent', currentUrl: 'https://example.com/' }} onControl={onControl} />);
    expect(screen.getByTestId('browser-task-card')).toBeInTheDocument();
    expect(screen.getByTestId('browser-pause-btn')).toBeInTheDocument();
    expect(screen.getByTestId('browser-take-control-btn')).toBeInTheDocument();
    expect(screen.getByTestId('browser-stop-btn')).toBeInTheDocument();
    expect(screen.getByText('https://example.com/')).toBeInTheDocument();
  });

  test('buttons report their actions', () => {
    const onControl = jest.fn();
    render(<BrowserTaskCard session={{ controlState: 'agent' }} onControl={onControl} />);
    fireEvent.click(screen.getByTestId('browser-pause-btn'));
    expect(onControl).toHaveBeenCalledWith('pause');
    fireEvent.click(screen.getByTestId('browser-take-control-btn'));
    expect(onControl).toHaveBeenCalledWith('take_control');
    fireEvent.click(screen.getByTestId('browser-stop-btn'));
    expect(onControl).toHaveBeenCalledWith('stop');
  });

  test('paused state offers resume instead of pause', () => {
    const onControl = jest.fn();
    render(<BrowserTaskCard session={{ controlState: 'paused' }} onControl={onControl} />);
    expect(screen.queryByTestId('browser-pause-btn')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('browser-resume-btn'));
    expect(onControl).toHaveBeenCalledWith('resume');
  });

  test('user-control state offers return control', () => {
    const onControl = jest.fn();
    render(<BrowserTaskCard session={{ controlState: 'user' }} onControl={onControl} />);
    expect(screen.getByText(/you have control/i)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('browser-return-control-btn'));
    expect(onControl).toHaveBeenCalledWith('return_control');
  });

  test('waiting state names the challenge and waits for the user', () => {
    render(
      <BrowserTaskCard
        session={{
          controlState: 'waiting_user',
          currentUrl: 'https://example.com/signup',
          challenge: { challenge: 'captcha', url: 'https://example.com/signup' },
        }}
        onControl={jest.fn()}
      />
    );
    expect(screen.getByText(/waiting for you/i)).toBeInTheDocument();
    expect(screen.getByText(/captcha/i)).toBeInTheDocument();
    // While parked there is no pause/take-control to offer.
    expect(screen.queryByTestId('browser-pause-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('browser-take-control-btn')).not.toBeInTheDocument();
  });

  test('stopped state disables the controls', () => {
    render(<BrowserTaskCard session={{ controlState: 'stopped' }} onControl={jest.fn()} />);
    expect(screen.getByTestId('browser-stop-btn')).toBeDisabled();
    expect(screen.getByTestId('browser-pause-btn')).toBeDisabled();
  });
});

describe('DraggableChatWindow browser card wiring (E8-6.1)', () => {
  test('no browser activity, no card', () => {
    renderChat({ messages: [{ id: 'u1', role: 'user', content: 'hi', isFinal: true, thoughts: [] }] });
    expect(screen.queryByTestId('browser-task-card')).not.toBeInTheDocument();
  });

  test('browser tool activity surfaces the card and controls reach the callback', () => {
    const onBrowserControl = jest.fn();
    renderChat({ messages: browserMessages, onBrowserControl });
    expect(screen.getByTestId('browser-task-card')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('browser-pause-btn'));
    expect(onBrowserControl).toHaveBeenCalledWith('pause');
    // The card reflects the click locally until the engine answers.
    expect(screen.getByTestId('browser-resume-btn')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('browser-take-control-btn'));
    expect(onBrowserControl).toHaveBeenCalledWith('take_control');
    fireEvent.click(screen.getByTestId('browser-stop-btn'));
    expect(onBrowserControl).toHaveBeenCalledWith('stop');
  });

  test('a challenge prop renders the waiting-for-you card', () => {
    renderChat({
      messages: browserMessages,
      browserSession: {
        controlState: 'waiting_user',
        currentUrl: 'https://example.com/signup',
        challenge: { challenge: 'two_factor', url: 'https://example.com/signup' },
      },
      onBrowserControl: jest.fn(),
    });
    expect(screen.getByText(/waiting for you/i)).toBeInTheDocument();
    expect(screen.getByText(/two.factor/i)).toBeInTheDocument();
  });
});
