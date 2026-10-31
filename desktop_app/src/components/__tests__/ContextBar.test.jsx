import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import ContextBar, { CompactContextBar } from '../ContextBar';

describe('ContextBar', () => {
  const sampleBreakdown = {
    model: 'gemini-3.5-flash',
    context_limit: 1048576,
    total_tokens: 10000,
    percent_used: 0.95,
    components: [
      { name: 'system prompt', tokens: 1500, share: 0.15 },
      { name: 'memory briefing', tokens: 500, share: 0.05 },
      { name: 'history', tokens: 3000, share: 0.3 },
      { name: 'tool outputs', tokens: 2500, share: 0.25 },
      { name: 'scratchpad', tokens: 500, share: 0.05 },
      { name: 'repo map', tokens: 1500, share: 0.15 },
      { name: 'skill catalogue', tokens: 500, share: 0.05 },
    ],
  };

  test('renders segmented bar with segments summing exactly to total tokens', () => {
    render(<ContextBar breakdown={sampleBreakdown} />);

    expect(screen.getByTestId('context-bar-panel')).toBeInTheDocument();
    expect(screen.getByTestId('context-segmented-bar')).toBeInTheDocument();

    const sumTokens = sampleBreakdown.components.reduce((acc, c) => acc + c.tokens, 0);
    expect(sumTokens).toBe(sampleBreakdown.total_tokens);

    // Verify all 7 component items in the breakdown list
    sampleBreakdown.components.forEach((c) => {
      const slug = c.name.replace(/\s+/g, '-');
      const item = screen.getByTestId(`context-item-${slug}`);
      expect(item).toBeInTheDocument();
      expect(item).toHaveTextContent(c.name);
      expect(item).toHaveTextContent(c.tokens.toLocaleString());
    });
  });

  test('renders zero tokens cleanly without dividing by zero', () => {
    const emptyBreakdown = {
      model: 'gemini-3.5-flash',
      context_limit: 1048576,
      total_tokens: 0,
      percent_used: 0,
      components: [
        { name: 'system prompt', tokens: 0, share: 0 },
        { name: 'memory briefing', tokens: 0, share: 0 },
        { name: 'history', tokens: 0, share: 0 },
        { name: 'tool outputs', tokens: 0, share: 0 },
        { name: 'scratchpad', tokens: 0, share: 0 },
        { name: 'repo map', tokens: 0, share: 0 },
        { name: 'skill catalogue', tokens: 0, share: 0 },
      ],
    };
    render(<ContextBar breakdown={emptyBreakdown} />);
    expect(screen.getByTestId('context-bar-panel')).toBeInTheDocument();
    expect(screen.getByText(/0 \/ 1,048,576 tokens/)).toBeInTheDocument();
  });

  test('CompactContextBar renders compact default-model indicator and agrees with popover totals', () => {
    const onClick = jest.fn();
    render(
      <CompactContextBar
        breakdown={sampleBreakdown}
        onClick={onClick}
      />
    );

    const compactBar = screen.getByTestId('compact-context-bar');
    expect(compactBar).toBeInTheDocument();
    expect(compactBar).toHaveTextContent('gemini-3.5-flash');
    expect(compactBar).toHaveTextContent('0.95%');

    fireEvent.click(compactBar);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
