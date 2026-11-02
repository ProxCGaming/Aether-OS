import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import UsageSettingsPanel from '../Settings/UsageSettingsPanel';

describe('UsageSettingsPanel', () => {
  const mockTotals = {
    total_cost_usd: 12.34,
    input_tokens: 50000,
    output_tokens: 15000,
    by_model: {
      'gemini-3.5-flash': {
        cost_usd: 10.50,
        input_tokens: 40000,
        output_tokens: 12000,
        calls: 8,
      },
      'llama3:8b': {
        cost_usd: 0.0,
        input_tokens: 5000,
        output_tokens: 1000,
        calls: 4,
        is_local: true,
      },
      'unpriced-custom': {
        cost_usd: 0.0,
        input_tokens: 5000,
        output_tokens: 2000,
        calls: 2,
        unpriced: true,
      },
    },
    by_agent: {
      supervisor: {
        cost_usd: 5.00,
        input_tokens: 20000,
        output_tokens: 5000,
        calls: 5,
      },
      coder: {
        cost_usd: 7.34,
        input_tokens: 30000,
        output_tokens: 10000,
        calls: 9,
      },
    },
  };

  test('renders totals by model and by agent accurately', () => {
    render(<UsageSettingsPanel initialTotals={mockTotals} todaySpend={2.50} dailyBudget={5.00} />);

    expect(screen.getByTestId('usage-settings-panel')).toBeInTheDocument();
    expect(screen.getByText('gemini-3.5-flash')).toBeInTheDocument();
    expect(screen.getByText('llama3:8b')).toBeInTheDocument();
    expect(screen.getByText('unpriced-custom')).toBeInTheDocument();

    expect(screen.getByText('supervisor')).toBeInTheDocument();
    expect(screen.getByText('coder')).toBeInTheDocument();
  });

  test('displays honest badges for local and unpriced models', () => {
    render(<UsageSettingsPanel initialTotals={mockTotals} todaySpend={2.50} dailyBudget={5.00} />);

    expect(screen.getByTestId('badge-local-llama3:8b')).toHaveTextContent('local');
    expect(screen.getByTestId('badge-unpriced-unpriced-custom')).toHaveTextContent('unpriced');
  });

  test('submits daily budget update via WebSocket', async () => {
    const mockSend = jest.fn();
    const wsRef = { current: { readyState: WebSocket.OPEN, send: mockSend } };

    render(<UsageSettingsPanel wsRef={wsRef} initialTotals={mockTotals} todaySpend={1.20} dailyBudget={null} />);

    const input = screen.getByTestId('daily-budget-input');
    const saveBtn = screen.getByTestId('daily-budget-save-btn');

    fireEvent.change(input, { target: { value: '15.50' } });
    fireEvent.click(saveBtn);

    expect(mockSend).toHaveBeenCalledWith(
      expect.stringContaining('"type":"DAILY_BUDGET_SET_REQUEST"')
    );
    expect(mockSend).toHaveBeenCalledWith(
      expect.stringContaining('"daily_budget_usd":15.5')
    );
  });

  // N14: Settings → Usage must also show the CURRENT working model's
  // context — used vs capacity, per component — from the same
  // breakdown payload the session header consumes.
  const mockBreakdown = {
    model: 'gemini-3.5-flash',
    context_limit: 1000,
    total_tokens: 400,
    percent_used: 40,
    partial: true,
    components: [
      { name: 'system prompt', tokens: 150, share: 0.375, unknown: false },
      { name: 'memory briefing', tokens: null, share: 0, unknown: true },
      { name: 'history', tokens: 250, share: 0.625, unknown: false },
      { name: 'tool outputs', tokens: 0, share: 0, unknown: false },
      { name: 'scratchpad', tokens: 0, share: 0, unknown: false },
      { name: 'repo map', tokens: 0, share: 0, unknown: false },
      { name: 'skill catalogue', tokens: 0, share: 0, unknown: false },
    ],
  };

  test('renders the current per-model context view from a breakdown payload (N14)', () => {
    render(
      <UsageSettingsPanel
        initialTotals={mockTotals}
        initialBreakdown={mockBreakdown}
        todaySpend={2.50}
        dailyBudget={5.00}
      />
    );

    const card = screen.getByTestId('usage-current-context');
    expect(card).toBeInTheDocument();
    expect(card).toHaveTextContent('gemini-3.5-flash');
    expect(card).toHaveTextContent('400 / 1,000 tokens');
    expect(card).toHaveTextContent('40%');

    // Capacity-relative shares, same basis as the header: history is
    // 250 of the 1,000-token window = 25%, not 62.5% of used tokens.
    const historyItem = screen.getByTestId('usage-context-item-history');
    expect(historyItem).toHaveTextContent('250');
    expect(historyItem).toHaveTextContent('25.0%');
    expect(screen.getByTestId('usage-context-segment-history').style.width).toBe('25%');

    // Unmeasured components stay unknown — never a fabricated number.
    const briefingItem = screen.getByTestId('usage-context-item-memory-briefing');
    expect(briefingItem).toHaveTextContent('unknown');
  });

  test('requests the breakdown over the existing context surface on mount (N14)', () => {
    const mockSend = jest.fn();
    let messageHandler = null;
    const wsRef = {
      current: {
        readyState: WebSocket.OPEN,
        send: mockSend,
        addEventListener: (type, fn) => { if (type === 'message') messageHandler = fn; },
        removeEventListener: () => {},
      },
    };

    render(
      <UsageSettingsPanel
        wsRef={wsRef}
        initialTotals={mockTotals}
        sessionId="sess-42"
        todaySpend={1.20}
      />
    );

    expect(mockSend).toHaveBeenCalledWith(
      expect.stringContaining('"type":"CONTEXT_BREAKDOWN_REQUEST"')
    );
    expect(mockSend).toHaveBeenCalledWith(
      expect.stringContaining('"session_id":"sess-42"')
    );

    act(() => {
      messageHandler({
        data: JSON.stringify({
          type: 'CONTEXT_BREAKDOWN_RESPONSE',
          payload: mockBreakdown,
        }),
      });
    });

    const card = screen.getByTestId('usage-current-context');
    expect(card).toHaveTextContent('gemini-3.5-flash');
    expect(card).toHaveTextContent('400 / 1,000 tokens');
  });

  test('shows soft alert banner when today spend exceeds daily budget without blocking', () => {
    render(
      <UsageSettingsPanel
        initialTotals={mockTotals}
        todaySpend={6.50}
        dailyBudget={5.00}
        budgetExceeded={true}
      />
    );

    const alertBanner = screen.getByTestId('budget-exceeded-banner');
    expect(alertBanner).toBeInTheDocument();
    expect(alertBanner).toHaveTextContent(/daily budget exceeded/i);
    expect(alertBanner).toHaveTextContent(/running tasks finish uninterrupted/i);
  });
});
