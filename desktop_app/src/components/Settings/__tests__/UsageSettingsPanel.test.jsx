/** D2-10: the Usage panel's Save Budget used to map an empty,
 * invalid or non-positive input to `null` — and `null` is the
 * CLEAR operation. Typing garbage, blanking the field, or entering
 * 0 and pressing Save silently removed an existing daily budget.
 * Save must refuse invalid input in the panel; clearing stays on
 * the separate Clear control. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import UsageSettingsPanel from '../UsageSettingsPanel';

function makeWsRef() {
  return {
    current: {
      readyState: 1, // WebSocket.OPEN
      send: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    },
  };
}

function renderPanel({ dailyBudget = 10 } = {}) {
  const wsRef = makeWsRef();
  const safeSend = jest.fn();
  render(
    <UsageSettingsPanel
      wsRef={wsRef}
      safeSend={safeSend}
      dailyBudget={dailyBudget}
      todaySpend={2}
    />
  );
  const budgetSends = () =>
    safeSend.mock.calls.filter(([type]) => type === 'DAILY_BUDGET_SET_REQUEST');
  return { wsRef, safeSend, budgetSends };
}

describe('UsageSettingsPanel Save Budget (D2-10)', () => {
  test('an emptied input is refused — Save must not become a silent Clear', () => {
    const { budgetSends } = renderPanel();
    fireEvent.change(screen.getByTestId('daily-budget-input'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('daily-budget-save-btn'));
    expect(budgetSends()).toHaveLength(0);
    expect(screen.getByTestId('daily-budget-input-error')).toHaveTextContent(/use Clear/i);
  });

  test('a zero budget is refused, not cleared', () => {
    const { budgetSends } = renderPanel();
    fireEvent.change(screen.getByTestId('daily-budget-input'), { target: { value: '0' } });
    fireEvent.click(screen.getByTestId('daily-budget-save-btn'));
    expect(budgetSends()).toHaveLength(0);
    expect(screen.getByTestId('daily-budget-input-error')).toBeInTheDocument();
  });

  test('a negative budget is refused, not cleared', () => {
    const { budgetSends } = renderPanel();
    fireEvent.change(screen.getByTestId('daily-budget-input'), { target: { value: '-5' } });
    // The number input's min=0 makes the browser's own validation
    // intercept a button click with -5 entered; submit the form
    // directly to exercise the handler's own refusal.
    fireEvent.submit(screen.getByTestId('daily-budget-save-btn').closest('form'));
    expect(budgetSends()).toHaveLength(0);
    expect(screen.getByTestId('daily-budget-input-error')).toBeInTheDocument();
  });

  test('a valid budget still saves with the parsed amount', () => {
    const { safeSend, budgetSends } = renderPanel();
    fireEvent.change(screen.getByTestId('daily-budget-input'), { target: { value: '7.50' } });
    fireEvent.click(screen.getByTestId('daily-budget-save-btn'));
    expect(budgetSends()).toHaveLength(1);
    expect(safeSend).toHaveBeenCalledWith('DAILY_BUDGET_SET_REQUEST', { daily_budget_usd: 7.5 });
    expect(screen.queryByTestId('daily-budget-input-error')).not.toBeInTheDocument();
  });

  test('the separate Clear control still clears the budget', () => {
    const { safeSend } = renderPanel();
    fireEvent.click(screen.getByText('Clear'));
    expect(safeSend).toHaveBeenCalledWith('DAILY_BUDGET_SET_REQUEST', { daily_budget_usd: null });
  });
});
