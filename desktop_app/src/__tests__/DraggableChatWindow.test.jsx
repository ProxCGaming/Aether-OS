import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, {
  getNodeStyle,
  PayloadDrawer,
  ToolActivityCard,
  NodeActivityCard,
  ThinkingAccordion,
} from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest (same as App.test.jsx and
// the sibling DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; the chat window calls it on new messages.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('getNodeStyle', () => {
  it('maps the supervisor node to the SUPERVISOR style', () => {
    expect(getNodeStyle('supervisor')).toEqual({
      color: '#c4b5fd',
      bg: 'rgba(167, 139, 250, 0.16)',
      border: 'rgba(167, 139, 250, 0.35)',
      name: 'SUPERVISOR',
    });
  });

  it('maps planner-like names (substring "plan") to PLANNER', () => {
    expect(getNodeStyle('planner').name).toBe('PLANNER');
    expect(getNodeStyle('planning_node').name).toBe('PLANNER');
    expect(getNodeStyle('planner').color).toBe('#93c5fd');
  });

  it('maps coder-like names to CODER', () => {
    expect(getNodeStyle('coder').name).toBe('CODER');
    expect(getNodeStyle('coder').color).toBe('#6ee7b7');
  });

  it('maps researcher-like names to RESEARCHER', () => {
    expect(getNodeStyle('researcher').name).toBe('RESEARCHER');
    expect(getNodeStyle('researcher').color).toBe('#7dd3fc');
  });

  it('maps execute_tool and other tool-ish names to EXECUTE_TOOL', () => {
    expect(getNodeStyle('execute_tool').name).toBe('EXECUTE_TOOL');
    expect(getNodeStyle('tool_runner').name).toBe('EXECUTE_TOOL');
    expect(getNodeStyle('execute_tool').color).toBe('#fde047');
  });

  it('maps user-like names to USER', () => {
    expect(getNodeStyle('user').name).toBe('USER');
    expect(getNodeStyle('user').color).toBe('#f472b6');
  });

  it('is case-insensitive', () => {
    expect(getNodeStyle('Supervisor').name).toBe('SUPERVISOR');
    expect(getNodeStyle('CODER').name).toBe('CODER');
  });

  it('falls back to the uppercased name with the neutral style', () => {
    const style = getNodeStyle('memory');
    expect(style.name).toBe('MEMORY');
    expect(style.color).toBe('#e2e8f0');
    expect(style.bg).toBe('rgba(255, 255, 255, 0.08)');
    expect(style.border).toBe('rgba(255, 255, 255, 0.15)');
  });

  it('falls back to NODE for an empty or missing name', () => {
    expect(getNodeStyle('').name).toBe('NODE');
    expect(getNodeStyle().name).toBe('NODE');
  });
});

describe('PayloadDrawer', () => {
  it('renders its title and keeps the body hidden while closed', () => {
    const { container } = render(
      <PayloadDrawer title="Request Payload" data={{ query: 'cats' }} />
    );
    expect(screen.getByText('Request Payload')).toBeInTheDocument();
    expect(container.querySelector('.payload-drawer-body')).not.toBeInTheDocument();
  });

  it('formats object data as pretty JSON when opened via the header', () => {
    const { container } = render(
      <PayloadDrawer title="Request Payload" data={{ query: 'cats', limit: 3 }} />
    );
    fireEvent.click(container.querySelector('.payload-drawer-header'));
    const code = container.querySelector('.payload-code-block');
    expect(code).toBeInTheDocument();
    expect(code.textContent).toBe(JSON.stringify({ query: 'cats', limit: 3 }, null, 2));
  });

  it('renders the JSON body immediately when defaultOpen is set', () => {
    const { container } = render(
      <PayloadDrawer title="Output" data={{ hits: 3 }} defaultOpen />
    );
    expect(container.querySelector('.payload-code-block').textContent).toContain('"hits": 3');
  });

  it('shows a param-count badge for object data', () => {
    const { rerender } = render(<PayloadDrawer title="T" data={{ a: 1, b: 2 }} />);
    expect(screen.getByText('2 params')).toBeInTheDocument();

    rerender(<PayloadDrawer title="T" data={{ a: 1 }} />);
    expect(screen.getByText('1 param')).toBeInTheDocument();
  });

  it('shows an item-count badge for arrays and a char-count badge for strings', () => {
    const { rerender } = render(<PayloadDrawer title="T" data={[1, 2, 3]} />);
    expect(screen.getByText('3 items')).toBeInTheDocument();

    rerender(<PayloadDrawer title="T" data="hello" />);
    expect(screen.getByText('5 chars')).toBeInTheDocument();
  });

  it('shows the supplied empty text for null data', () => {
    render(<PayloadDrawer title="Result" data={null} defaultOpen emptyText="Nothing to show" />);
    expect(screen.getByText('Nothing to show')).toBeInTheDocument();
  });

  it('shows the default empty text for missing data', () => {
    render(<PayloadDrawer title="Result" defaultOpen />);
    expect(screen.getByText('No payload')).toBeInTheDocument();
  });

  it('shows the no-parameters notice for an empty object with "{}" empty text', () => {
    render(<PayloadDrawer title="Args" data={{}} defaultOpen emptyText="{}" />);
    expect(screen.getByText('{} (No parameters required)')).toBeInTheDocument();
  });
});

describe('ToolActivityCard', () => {
  it('renders nothing for a null payload', () => {
    const { container } = render(<ToolActivityCard payload={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders the tool name and status for a completed payload', () => {
    const { container } = render(
      <ToolActivityCard
        payload={{
          tool_name: 'web_search',
          tool_args: { query: 'aether os' },
          status: 'completed',
          result: { hits: 3 },
          from_node: 'planner',
          to_node: 'execute_tool',
        }}
      />
    );
    expect(screen.getByText('web_search')).toBeInTheDocument();
    expect(screen.getByText('completed')).toBeInTheDocument();

    const badge = container.querySelector('.tool-route-badge');
    expect(badge.textContent).toContain('PLANNER');
    expect(badge.textContent).toContain('EXECUTE_TOOL');

    // The output drawer is open by default for finished tools and shows
    // the result formatted as JSON.
    const titles = [...container.querySelectorAll('.payload-title')].map((el) => el.textContent);
    expect(titles).toContain('📤 Output Payload');
    expect(container.querySelector('.payload-code-block').textContent).toContain('"hits": 3');
  });

  it('defaults the route and tool name when fields are missing', () => {
    const { container } = render(
      <ToolActivityCard payload={{ status: 'completed', result: 'ok' }} />
    );
    expect(screen.getByText('unknown_tool')).toBeInTheDocument();
    const badge = container.querySelector('.tool-route-badge');
    expect(badge.textContent).toContain('AGENT');
    expect(badge.textContent).toContain('EXECUTE_TOOL');
  });

  it('shows the awaiting message and pending status for a pending payload', () => {
    render(
      <ToolActivityCard payload={{ tool_name: 'shell', status: 'pending', tool_args: {} }} />
    );
    expect(screen.getByText('pending')).toBeInTheDocument();
    expect(
      screen.getByText('Executing tool in sandbox... awaiting output payload')
    ).toBeInTheDocument();
  });

  it('marks a failed payload with the failed status and card styling', () => {
    const { container } = render(
      <ToolActivityCard payload={{ tool_name: 'shell', status: 'failed', result: null }} />
    );
    expect(screen.getByText('failed')).toBeInTheDocument();
    expect(container.querySelector('.tool-activity-card.failed')).toBeInTheDocument();
  });

  it('renders the approval panel and wires approve/reject to onApproveTool', () => {
    const onApproveTool = jest.fn();
    render(
      <ToolActivityCard
        payload={{ tool_name: 'shell', status: 'pending', tool_args: { cmd: 'ls' } }}
        pendingApproval={{ id: 'approval-1', approval_key: 'key-1' }}
        onApproveTool={onApproveTool}
      />
    );
    expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();
    expect(screen.getByText('approval required')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));
    expect(onApproveTool).toHaveBeenCalledWith('approval-1', true, 'key-1');

    fireEvent.click(screen.getByRole('button', { name: 'Reject Request' }));
    expect(onApproveTool).toHaveBeenCalledWith('approval-1', false, 'key-1');
  });
});

describe('NodeActivityCard', () => {
  it('renders nothing for a null activity', () => {
    const { container } = render(<NodeActivityCard activity={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders node badges, the action pill and the reason', () => {
    render(
      <NodeActivityCard
        activity={{
          from_node: 'supervisor',
          to_node: 'coder',
          action: 'delegate_task',
          reason: 'The user asked for a new component',
          status: 'completed',
        }}
      />
    );
    expect(screen.getByText('SUPERVISOR')).toBeInTheDocument();
    expect(screen.getByText('CODER')).toBeInTheDocument();
    expect(screen.getByText('DELEGATE TASK')).toBeInTheDocument();
    expect(screen.getByText(/The user asked for a new component/)).toBeInTheDocument();
  });

  it('includes payload drawers for non-empty input/output payloads', () => {
    const { container } = render(
      <NodeActivityCard
        activity={{
          from_node: 'planner',
          to_node: 'researcher',
          input_payload: { topic: 'memory' },
          output_payload: { summary: 'done' },
        }}
      />
    );
    const titles = [...container.querySelectorAll('.payload-title')].map((el) => el.textContent);
    expect(titles).toContain('📥 Input State / Payload');
    expect(titles).toContain('📤 Output State / Payload');
  });
});

describe('ThinkingAccordion', () => {
  it('renders nothing when there are no thoughts and nothing is running', () => {
    const { container } = render(
      <ThinkingAccordion thoughts={[]} isThinking={false} hasError={false} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the thinking header while the agent is thinking', () => {
    render(<ThinkingAccordion thoughts={[]} isThinking hasError={false} />);
    expect(screen.getByText('Agent is thinking...')).toBeInTheDocument();
  });

  it('shows the stopped header after an error', () => {
    render(<ThinkingAccordion thoughts={[]} isThinking={false} hasError />);
    expect(screen.getByText('Task Stopped')).toBeInTheDocument();
  });

  it('summarises recorded node activity as pipeline steps', () => {
    render(
      <ThinkingAccordion
        thoughts={[
          {
            type: 'NODE_ACTIVITY',
            payload: { from_node: 'supervisor', to_node: 'planner', action: 'delegate' },
          },
        ]}
        isThinking={false}
        hasError={false}
      />
    );
    expect(screen.getByText('Execution Pipeline (1 steps)')).toBeInTheDocument();
  });
});

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

describe('DraggableChatWindow (default export)', () => {
  it('renders the empty-session placeholder and the composer', () => {
    renderChat();
    expect(
      screen.getByText('No active session. Open a project or start a new chat.')
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
    expect(screen.getByText('Select Model...')).toBeInTheDocument();
  });

  it('renders a user message', () => {
    renderChat({ messages: [{ role: 'user', content: 'Hello Aether' }] });
    expect(screen.getByText('Hello Aether')).toBeInTheDocument();
    expect(
      screen.queryByText('No active session. Open a project or start a new chat.')
    ).not.toBeInTheDocument();
  });

  it('renders an assistant message through the markdown renderer', () => {
    renderChat({ messages: [{ role: 'assistant', content: 'Here is the plan' }] });
    const stub = screen.getByTestId('markdown-stub');
    expect(stub.textContent).toContain('Here is the plan');
  });

  it('renders a system message as a notice row', () => {
    renderChat({ messages: [{ role: 'system', content: 'Engine reconnected' }] });
    expect(screen.getByText('Engine reconnected')).toBeInTheDocument();
  });

  it('sends the typed message on Enter and clears the composer', () => {
    const onSendMessage = jest.fn();
    renderChat({ onSendMessage });

    const composer = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(composer, { target: { value: '  build the thing  ' } });
    fireEvent.keyDown(composer, { key: 'Enter', shiftKey: false });

    expect(onSendMessage).toHaveBeenCalledWith('build the thing');
    expect(composer.value).toBe('');
  });

  it('does not send while the agent is thinking', () => {
    const onSendMessage = jest.fn();
    renderChat({ onSendMessage, isThinking: true });

    const composer = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(composer, { target: { value: 'hello' } });
    fireEvent.keyDown(composer, { key: 'Enter', shiftKey: false });

    expect(onSendMessage).not.toHaveBeenCalled();
  });
});
