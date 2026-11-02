import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

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

// E8-6.2: a browser confirmation names the EXACT action on the card
// (amount + merchant from the page), and for non-payment categories
// the owner can grant a per-task envelope while approving.
describe('Browser confirmation card (E8-6.2)', () => {
  const paymentMessage = {
    role: 'tool_approval',
    id: 'appr-pay',
    tool_name: 'browser_click',
    tool_args: { selector: '#pay-btn' },
    approval_key: 'key-pay',
    browser_confirmation: {
      category: 'payments',
      description: 'Pay ₹499 to Merchant',
      site_host: 'shop.example.com',
      site_name: 'Merchant',
      amount: 499,
      amount_text: '₹499',
      currency: 'INR',
      tool_name: 'browser_click',
    },
  };

  const sendMessage = {
    role: 'tool_approval',
    id: 'appr-send',
    tool_name: 'browser_click',
    tool_args: { selector: '#send-btn' },
    approval_key: 'key-send',
    browser_confirmation: {
      category: 'sends_publishes',
      description: 'Send message on Webmail',
      site_host: 'mail.example.com',
      site_name: 'Webmail',
      amount: null,
      amount_text: null,
      currency: null,
      tool_name: 'browser_click',
    },
  };

  it('renders the exact action for a payment confirmation', () => {
    renderChat({ messages: [paymentMessage] });
    expect(screen.getByTestId('browser-confirmation-description')).toHaveTextContent(
      'Pay ₹499 to Merchant'
    );
    // Payments never offer an envelope grant.
    expect(screen.queryByTestId('browser-envelope-checkbox')).not.toBeInTheDocument();
    expect(screen.getByText(/payments are always confirmed individually/i)).toBeInTheDocument();
  });

  it('approving a payment sends no envelope grant', () => {
    const { handlers } = renderChat({ messages: [paymentMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-pay', true, 'key-pay');
  });

  it('a non-payment confirmation offers the per-task envelope grant', () => {
    const { handlers } = renderChat({ messages: [sendMessage] });
    expect(screen.getByTestId('browser-confirmation-description')).toHaveTextContent(
      'Send message on Webmail'
    );
    fireEvent.click(screen.getByTestId('browser-envelope-checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    const call = handlers.onApproveTool.mock.calls[0];
    expect(call[0]).toBe('appr-send');
    expect(call[1]).toBe(true);
    expect(call[2]).toBe('key-send');
    expect(call[5]).toEqual({ grant: true });
  });

  it('without the checkbox, approval carries no envelope grant', () => {
    const { handlers } = renderChat({ messages: [sendMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-send', true, 'key-send');
  });
});
