import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { ScratchpadPanel } from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// The mermaid package is mocked: the panel must initialise it with
// securityLevel 'strict', call render with the source, and insert the
// returned SVG. Real diagram layout is not jsdom's business.
jest.mock('mermaid', () => ({
  __esModule: true,
  default: {
    initialize: jest.fn(),
    render: jest.fn(),
  },
}));
import mermaid from 'mermaid';

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

// Epic 1, E1-17: the scratchpad panel renders engine scratchpad
// content by kind (svg markup / mermaid diagram / text), collapses,
// dismisses, and reopens when a new scratchpad arrives.
describe('ScratchpadPanel (Epic 1, E1-17)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mermaid.render.mockResolvedValue({
      svg: '<svg data-testid="mermaid-out"><text>diagram</text></svg>',
    });
  });

  it('renders nothing without a scratchpad', () => {
    render(<ScratchpadPanel scratchpad={null} />);
    expect(screen.queryByTestId('scratchpad-panel')).not.toBeInTheDocument();
  });

  it('renders svg markup inside the panel with kind + agent in the header', () => {
    render(
      <ScratchpadPanel
        scratchpad={{ kind: 'svg', content: '<svg data-testid="pad-svg"><circle r="4" /></svg>', agent: 'coder' }}
      />
    );
    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();
    expect(screen.getByText('Scratchpad')).toBeInTheDocument();
    expect(screen.getByText('svg')).toBeInTheDocument();
    expect(screen.getByText('coder')).toBeInTheDocument();
    expect(screen.getByTestId('pad-svg')).toBeInTheDocument();
  });

  it('renders text scratchpads in a pre block', () => {
    render(<ScratchpadPanel scratchpad={{ kind: 'text', content: 'step 1: sketch\nstep 2: verify', agent: 'planner' }} />);
    const pre = document.querySelector('pre.scratchpad-text');
    expect(pre).toBeTruthy();
    expect(pre.textContent).toContain('step 1: sketch');
  });

  it('renders a mermaid scratchpad through mermaid.render with strict security', async () => {
    render(<ScratchpadPanel scratchpad={{ kind: 'mermaid', content: 'graph TD; A-->B', agent: 'planner' }} />);

    expect(await screen.findByTestId('mermaid-out')).toBeInTheDocument();
    expect(mermaid.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: 'strict', startOnLoad: false })
    );
    expect(mermaid.render).toHaveBeenCalledWith(expect.any(String), 'graph TD; A-->B');
    expect(document.querySelector('.scratchpad-mermaid')).toBeTruthy();
  });

  it('falls back to the source with a note when mermaid rendering fails', async () => {
    mermaid.render.mockRejectedValueOnce(new Error('bad syntax'));
    render(<ScratchpadPanel scratchpad={{ kind: 'mermaid', content: 'graph TD; A---', agent: 'coder' }} />);

    expect(await screen.findByText(/Diagram could not be rendered/)).toBeInTheDocument();
    const pre = document.querySelector('pre.scratchpad-source');
    expect(pre).toBeTruthy();
    expect(pre.textContent).toBe('graph TD; A---');
  });

  it('collapses and re-expands from the header toggle', () => {
    render(
      <ScratchpadPanel
        scratchpad={{ kind: 'svg', content: '<svg data-testid="pad-svg-2"><rect /></svg>', agent: 'coder' }}
      />
    );
    expect(screen.getByTestId('pad-svg-2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Collapse scratchpad' }));
    expect(screen.queryByTestId('pad-svg-2')).not.toBeInTheDocument();
    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Expand scratchpad' }));
    expect(screen.getByTestId('pad-svg-2')).toBeInTheDocument();
  });

  it('dismiss hides the panel and a new scratchpad object brings it back', () => {
    const { rerender } = render(
      <ScratchpadPanel scratchpad={{ kind: 'text', content: 'first sketch', agent: 'coder' }} />
    );
    expect(screen.getByText('first sketch')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss scratchpad' }));
    expect(screen.queryByTestId('scratchpad-panel')).not.toBeInTheDocument();

    rerender(<ScratchpadPanel scratchpad={{ kind: 'text', content: 'second sketch', agent: 'planner' }} />);
    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();
    expect(screen.getByText('second sketch')).toBeInTheDocument();
  });

  it('the chat window renders the panel from its scratchpad prop', () => {
    renderChat({
      scratchpad: { kind: 'svg', content: '<svg data-testid="pad-svg-3"><rect /></svg>', agent: 'researcher' },
    });
    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();
    expect(screen.getByTestId('pad-svg-3')).toBeInTheDocument();
    expect(screen.getByText('researcher')).toBeInTheDocument();
  });
});
