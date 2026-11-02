import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { ToolActivityCard } from '../DraggableChatWindow';

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

const SENDS_CONFIRMATION = {
  category: 'sends_publishes',
  description: 'Send message on Webmail',
  site_host: 'mail.example.com',
  site_name: 'Webmail',
  amount: null,
  amount_text: null,
  currency: null,
  tool_name: 'browser_click',
};

const PAYMENTS_CONFIRMATION = {
  category: 'payments',
  description: 'Pay ₹499 to Merchant',
  site_host: 'shop.example.com',
  site_name: 'Merchant',
  amount: 499,
  amount_text: '₹499',
  currency: 'INR',
  tool_name: 'browser_click',
};

function EnvelopeHarness({ onApproveTool, confirmation }) {
  const [checked, setChecked] = React.useState(false);
  return (
    <ToolActivityCard
      payload={{
        tool_name: 'browser_click',
        tool_args: { selector: '#send-btn' },
        status: 'pending',
      }}
      pendingApproval={{
        id: 'appr-c1',
        approval_key: 'key-c1',
        tool_name: 'browser_click',
        tool_args: { selector: '#send-btn' },
        browser_confirmation: confirmation,
      }}
      onApproveTool={onApproveTool}
      envelopeGrantChecked={checked}
      onEnvelopeGrantCheckedChange={setChecked}
    />
  );
}

function renderChat(props = {}) {
  const handlers = {
    onSendMessage: jest.fn(),
    onStopTask: jest.fn(),
    onApproveTool: jest.fn(),
    onApprovePlugin: jest.fn(),
    onChangeModel: jest.fn(),
    onDetach: jest.fn(),
    ...props.handlers,
  };
  delete props.handlers;
  const utils = render(
    <DraggableChatWindow
      messages={[]}
      status="Online"
      isThinking={false}
      providers={[]}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...handlers}
      {...props}
    />
  );
  return { ...utils, handlers };
}

// N11 (Epic 8 Astra review): the envelope grant existed only on the
// standalone approval card — the compact ToolActivityCard approved
// without the checkbox and without the envelope payload argument, so
// the same approval offered different powers depending on which
// surface rendered it. Both surfaces must share one control + payload.
describe('Compact approval card envelope grant (N11)', () => {
  test('compact card offers the envelope checkbox and sends the grant as the 6th argument', () => {
    const onApproveTool = jest.fn();
    render(<EnvelopeHarness onApproveTool={onApproveTool} confirmation={SENDS_CONFIRMATION} />);

    const checkbox = screen.getByTestId('browser-envelope-checkbox');
    expect(screen.getByTestId('browser-confirmation-description')).toHaveTextContent(
      'Send message on Webmail'
    );
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));

    expect(onApproveTool).toHaveBeenCalledTimes(1);
    const call = onApproveTool.mock.calls[0];
    expect(call[0]).toBe('appr-c1');
    expect(call[1]).toBe(true);
    expect(call[2]).toBe('key-c1');
    expect(call[5]).toEqual({ grant: true });
  });

  test('compact card without the checkbox ticked sends no envelope', () => {
    const onApproveTool = jest.fn();
    render(<EnvelopeHarness onApproveTool={onApproveTool} confirmation={SENDS_CONFIRMATION} />);

    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));

    expect(onApproveTool).toHaveBeenCalledTimes(1);
    const call = onApproveTool.mock.calls[0];
    expect(call[5]).toBeUndefined();
  });

  test('payments are never envelope-eligible on the compact card either', () => {
    const onApproveTool = jest.fn();
    render(<EnvelopeHarness onApproveTool={onApproveTool} confirmation={PAYMENTS_CONFIRMATION} />);

    expect(screen.queryByTestId('browser-envelope-checkbox')).not.toBeInTheDocument();
    expect(screen.getByText(/always confirmed individually/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));
    const call = onApproveTool.mock.calls[0];
    expect(call[5]).toBeUndefined();
  });

  test('full chat: the compact card inside the thinking accordion carries the grant', () => {
    const approvalMessage = {
      role: 'tool_approval',
      id: 'appr-c9',
      tool_name: 'browser_click',
      tool_args: { selector: '#send-btn' },
      approval_key: 'key-c9',
      browser_confirmation: SENDS_CONFIRMATION,
    };
    const agentMessage = {
      role: 'assistant',
      id: 'agent-1',
      content: 'Working on it.',
      isFinal: false,
      thoughts: [
        {
          type: 'TOOL_ACTIVITY',
          payload: {
            tool_name: 'browser_click',
            tool_args: { selector: '#send-btn' },
            status: 'pending',
          },
        },
      ],
    };
    const { handlers, container } = renderChat({
      messages: [agentMessage, approvalMessage],
    });

    const compactCard = container.querySelector('.tool-activity-card');
    expect(compactCard).not.toBeNull();
    const compactCheckbox = compactCard.querySelector(
      '[data-testid="browser-envelope-checkbox"]'
    );
    expect(compactCheckbox).not.toBeNull();
    fireEvent.click(compactCheckbox);
    fireEvent.click(within(compactCard).getByRole('button', { name: 'Approve & Execute' }));

    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    const call = handlers.onApproveTool.mock.calls[0];
    expect(call[0]).toBe('appr-c9');
    expect(call[5]).toEqual({ grant: true });
  });
});
