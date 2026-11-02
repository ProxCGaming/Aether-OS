import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import CostHeaderPopover, { formatUsd, groupEntriesByTask } from '../CostHeaderPopover';

// Fixture rows shaped exactly like aether_engine.cost.ledger rows
// (the USAGE_LEDGER_QUERY_RESPONSE entries payload).
const ENTRIES = [
  { id: 'e1', timestamp: 1000, session_id: 's1', task_id: 't1', agent_name: 'coder',
    model_name: 'gemini-3.5-flash', provider_name: 'google_gemini',
    input_tokens: 1000, output_tokens: 500, total_tokens: 1500,
    cost_usd: 0.0015, is_local: 0, pricing_status: 'priced', task_class: 'graph' },
  { id: 'e2', timestamp: 1001, session_id: 's1', task_id: 't1', agent_name: 'supervisor',
    model_name: 'gemini-3.5-flash', provider_name: 'google_gemini',
    input_tokens: 2000, output_tokens: 1000, total_tokens: 3000,
    cost_usd: 0.003, is_local: 0, pricing_status: 'priced', task_class: 'graph' },
  { id: 'e3', timestamp: 1002, session_id: 's1', task_id: 't1', agent_name: 'fast_chat',
    model_name: 'llama3.1', provider_name: 'ollama',
    input_tokens: 500, output_tokens: 100, total_tokens: 600,
    cost_usd: 0, is_local: 1, pricing_status: 'priced', task_class: 'fast_chat' },
  { id: 'e4', timestamp: 2000, session_id: 's1', task_id: 't2', agent_name: 'coder',
    model_name: 'acme/turbo-9', provider_name: 'custom_openai',
    input_tokens: 300, output_tokens: 60, total_tokens: 360,
    cost_usd: 0, is_local: 0, pricing_status: 'unpriced', task_class: 'graph' },
];

describe('formatUsd', () => {
  it('formats zero, sub-cent, and normal amounts distinctly', () => {
    expect(formatUsd(0)).toBe('$0.00');
    expect(formatUsd(0.0045)).toBe('$0.0045');
    expect(formatUsd(1.5)).toBe('$1.50');
    expect(formatUsd(2)).toBe('$2.00');
  });
});

describe('groupEntriesByTask', () => {
  it('groups entries by task, newest first, with sums matching the rows', () => {
    const tasks = groupEntriesByTask(ENTRIES);
    expect(tasks.map(t => t.task_id)).toEqual(['t2', 't1']);

    const t1 = tasks.find(t => t.task_id === 't1');
    expect(t1.cost_usd).toBeCloseTo(0.0045, 10);
    expect(t1.input_tokens).toBe(3500);
    expect(t1.output_tokens).toBe(1600);

    const gemini = t1.by_model['gemini-3.5-flash'];
    expect(gemini.cost_usd).toBeCloseTo(0.0045, 10);
    expect(gemini.calls).toBe(2);
    const local = t1.by_model['llama3.1'];
    expect(local.cost_usd).toBe(0);
    expect(local.is_local).toBe(true);

    const t2 = tasks.find(t => t.task_id === 't2');
    expect(t2.by_model['acme/turbo-9'].unpriced).toBe(true);

    // The grouped total reconciles exactly with the raw entry sum.
    const groupedTotal = tasks.reduce((sum, t) => sum + t.cost_usd, 0);
    const entryTotal = ENTRIES.reduce((sum, e) => sum + e.cost_usd, 0);
    expect(groupedTotal).toBeCloseTo(entryTotal, 10);
  });

  it('handles an empty ledger', () => {
    expect(groupEntriesByTask([])).toEqual([]);
    expect(groupEntriesByTask(null)).toEqual([]);
  });
});

describe('CostHeaderPopover', () => {
  it('shows the session total, per-task costs, and per-model attribution', () => {
    const tasks = groupEntriesByTask(ENTRIES);
    const total = ENTRIES.reduce((sum, e) => sum + e.cost_usd, 0);
    render(<CostHeaderPopover tasks={tasks} totalCostUsd={total} runningTaskIds={['t1']} />);

    expect(screen.getByTestId('cost-popover-total')).toHaveTextContent('$0.0045');
    expect(screen.getByTestId('cost-task-total-t1')).toHaveTextContent('$0.0045');
    expect(screen.getByTestId('cost-task-total-t2')).toHaveTextContent('$0.00');

    // Per-model attribution inside the task.
    const geminiRow = screen.getByTestId('cost-model-t1-gemini-3.5-flash');
    expect(geminiRow).toHaveTextContent('gemini-3.5-flash');
    expect(geminiRow).toHaveTextContent('$0.0045');
    const localRow = screen.getByTestId('cost-model-t1-llama3.1');
    expect(localRow).toHaveTextContent('$0.00');
    expect(localRow).toHaveTextContent(/local/i);

    // Unpriced models are labelled, never passed off as free.
    const unpricedRow = screen.getByTestId('cost-model-t2-acme/turbo-9');
    expect(unpricedRow).toHaveTextContent(/unpriced/i);

    // The running task is badged; the finished one is not.
    expect(screen.getByTestId('cost-task-running-t1')).toBeInTheDocument();
    expect(screen.queryByTestId('cost-task-running-t2')).not.toBeInTheDocument();
  });

  it('renders an honest empty state', () => {
    render(<CostHeaderPopover tasks={[]} totalCostUsd={0} />);
    expect(screen.getByTestId('cost-popover-total')).toHaveTextContent('$0.00');
    expect(screen.getByText(/no model calls recorded/i)).toBeInTheDocument();
  });
});
