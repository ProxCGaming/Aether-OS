import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 8, E8-A8.1a: the chat header cost icon ticks up live on
// TASK_COST_UPDATE, and the popover's figures reconcile with the
// USAGE_LEDGER_QUERY_RESPONSE (the ledger rows) for the session.

jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('App cost ledger header (E8-A8.1a)', () => {
  let engineListeners;
  let electronAPI;

  beforeEach(() => {
    engineListeners = [];
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connectAndRender = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  const costUpdate = (overrides = {}) => ({
    type: 'TASK_COST_UPDATE',
    payload: {
      session_id: 's1',
      task_id: 't1',
      delta_cost_usd: 0.0015,
      task_total_cost_usd: 0.0015,
      input_tokens: 1000,
      output_tokens: 500,
      model: 'gemini-3.5-flash',
      ...overrides,
    },
  });

  it('ticks the header total up live as task cost updates arrive', async () => {
    await connectAndRender();
    expect(screen.getByTestId('cost-header-total')).toHaveTextContent('$0.00');

    emitEngineMessage(costUpdate());
    expect(screen.getByTestId('cost-header-total')).toHaveTextContent('$0.0015');

    emitEngineMessage(costUpdate({ delta_cost_usd: 0.003, task_total_cost_usd: 0.0045, input_tokens: 2000, output_tokens: 1000 }));
    expect(screen.getByTestId('cost-header-total')).toHaveTextContent('$0.0045');

    // Updates for another session never touch this header.
    emitEngineMessage(costUpdate({ session_id: 'other', task_id: 't9', delta_cost_usd: 9, task_total_cost_usd: 9 }));
    expect(screen.getByTestId('cost-header-total')).toHaveTextContent('$0.0045');
  });

  it('popover reconciles with the ledger query response', async () => {
    await connectAndRender();

    emitEngineMessage(costUpdate());
    fireEvent.click(screen.getByTestId('cost-header-btn'));

    // The engine answers the popover's ledger query with the rows.
    emitEngineMessage({
      type: 'USAGE_LEDGER_QUERY_RESPONSE',
      payload: {
        entries: [
          { id: 'e1', timestamp: 1000, session_id: 's1', task_id: 't1', agent_name: 'coder',
            model_name: 'gemini-3.5-flash', provider_name: 'google_gemini',
            input_tokens: 1000, output_tokens: 500, total_tokens: 1500,
            cost_usd: 0.0015, is_local: 0, pricing_status: 'priced', task_class: 'graph' },
          { id: 'e2', timestamp: 1001, session_id: 's1', task_id: 't1', agent_name: 'fast_chat',
            model_name: 'llama3.1', provider_name: 'ollama',
            input_tokens: 500, output_tokens: 100, total_tokens: 600,
            cost_usd: 0, is_local: 1, pricing_status: 'priced', task_class: 'fast_chat' },
        ],
        totals: { total_cost_usd: 0.0015, input_tokens: 1500, output_tokens: 600, by_model: {}, by_agent: {} },
      },
    });

    expect(screen.getByTestId('cost-popover-total')).toHaveTextContent('$0.0015');
    expect(screen.getByTestId('cost-task-total-t1')).toHaveTextContent('$0.0015');
    expect(screen.getByTestId('cost-model-t1-gemini-3.5-flash')).toHaveTextContent('$0.0015');
    expect(screen.getByTestId('cost-model-t1-llama3.1')).toHaveTextContent(/local/i);
    // Header and popover agree.
    expect(screen.getByTestId('cost-header-total')).toHaveTextContent('$0.0015');
  });

  it('renders persistent compact default-model context bar and opens breakdown popover', async () => {
    await connectAndRender();

    // Default model compact bar is always present in header
    const compactBar = screen.getByTestId('compact-context-bar');
    expect(compactBar).toBeInTheDocument();

    // Push CONTEXT_BREAKDOWN_RESPONSE
    emitEngineMessage({
      type: 'CONTEXT_BREAKDOWN_RESPONSE',
      payload: {
        model: 'gemini-3.5-flash',
        context_limit: 1048576,
        total_tokens: 2500,
        percent_used: 0.24,
        components: [
          { name: 'system prompt', tokens: 500, share: 0.2 },
          { name: 'memory briefing', tokens: 0, share: 0.0 },
          { name: 'history', tokens: 1000, share: 0.4 },
          { name: 'tool outputs', tokens: 500, share: 0.2 },
          { name: 'scratchpad', tokens: 0, share: 0.0 },
          { name: 'repo map', tokens: 300, share: 0.12 },
          { name: 'skill catalogue', tokens: 200, share: 0.08 },
        ],
      },
    });

    expect(compactBar).toHaveTextContent('gemini-3.5-flash');
    expect(compactBar).toHaveTextContent('0.24%');

    // Click Context button to open popover
    fireEvent.click(screen.getByTestId('context-header-btn'));
    expect(screen.getByTestId('context-bar-panel')).toBeInTheDocument();
    expect(screen.getByTestId('context-segmented-bar')).toBeInTheDocument();
    expect(screen.getByTestId('context-item-system-prompt')).toHaveTextContent('500');
    expect(screen.getByTestId('context-item-repo-map')).toHaveTextContent('300');
  });

  it('displays soft alert notice when daily budget is exceeded', async () => {
    await connectAndRender();

    // Budget exceeded alert arrives
    emitEngineMessage({
      type: 'DAILY_BUDGET_EXCEEDED',
      payload: {
        daily_budget_usd: 5.00,
        today_spend_usd: 6.25,
      },
    });

    // Open cost popover
    fireEvent.click(screen.getByTestId('cost-header-btn'));
    const alert = screen.getByTestId('budget-exceeded-alert');
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent(/daily budget exceeded/i);
    expect(alert).toHaveTextContent('$6.25');
  });
});
