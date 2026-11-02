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

  test('renders an unmeasured component as unknown, never as a number (M20)', () => {
    const partialBreakdown = {
      model: 'gemini-3.5-flash',
      context_limit: 1048576,
      total_tokens: 3000,
      percent_used: 0.29,
      partial: true,
      components: [
        { name: 'system prompt', tokens: null, share: 0, unknown: true },
        { name: 'memory briefing', tokens: null, share: 0, unknown: true },
        { name: 'history', tokens: 3000, share: 1 },
        { name: 'tool outputs', tokens: 0, share: 0 },
        { name: 'scratchpad', tokens: 0, share: 0 },
        { name: 'repo map', tokens: 0, share: 0 },
        { name: 'skill catalogue', tokens: 0, share: 0 },
      ],
    };
    render(<ContextBar breakdown={partialBreakdown} />);

    const systemItem = screen.getByTestId('context-item-system-prompt');
    expect(systemItem).toHaveTextContent('unknown');
    expect(systemItem).not.toHaveTextContent('150');
    const historyItem = screen.getByTestId('context-item-history');
    expect(historyItem).toHaveTextContent('3,000');
  });

  // N14: detailed shares must be of the model's CAPACITY (the window),
  // not of the tokens used so far — a component filling 25% of the
  // window renders 25% however full the window is overall.
  test('segment widths and list shares are capacity-relative, not used-relative (N14)', () => {
    const capacityBreakdown = {
      model: 'gemini-3.5-flash',
      context_limit: 1000,
      total_tokens: 400,
      percent_used: 40,
      components: [
        { name: 'system prompt', tokens: 150, share: 0.375 },
        { name: 'memory briefing', tokens: 0, share: 0 },
        { name: 'history', tokens: 250, share: 0.625 },
        { name: 'tool outputs', tokens: 0, share: 0 },
        { name: 'scratchpad', tokens: 0, share: 0 },
        { name: 'repo map', tokens: 0, share: 0 },
        { name: 'skill catalogue', tokens: 0, share: 0 },
      ],
    };
    render(<ContextBar breakdown={capacityBreakdown} />);

    // 250 of a 1,000-token window is 25% of the window — NOT 62.5%
    // (its share of the 400 tokens used).
    const historySegment = screen.getByTestId('context-segment-history');
    expect(historySegment.style.width).toBe('25%');
    expect(historySegment.title).toContain('25.0% of window');
    const systemSegment = screen.getByTestId('context-segment-system-prompt');
    expect(systemSegment.style.width).toBe('15%');

    const historyItem = screen.getByTestId('context-item-history');
    expect(historyItem).toHaveTextContent('25.0%');
    expect(historyItem).not.toHaveTextContent('62.5%');

    // Capacity is known, so no fallback note.
    expect(screen.queryByTestId('context-share-basis-note')).not.toBeInTheDocument();
  });

  test('unknown components are excluded from shares without distorting the others (N14)', () => {
    const partialCapacityBreakdown = {
      model: 'gemini-3.5-flash',
      context_limit: 1000,
      total_tokens: 250,
      percent_used: 25,
      partial: true,
      components: [
        { name: 'system prompt', tokens: null, share: 0, unknown: true },
        { name: 'memory briefing', tokens: null, share: 0, unknown: true },
        { name: 'history', tokens: 250, share: 1 },
        { name: 'tool outputs', tokens: 0, share: 0 },
        { name: 'scratchpad', tokens: 0, share: 0 },
        { name: 'repo map', tokens: 0, share: 0 },
        { name: 'skill catalogue', tokens: 0, share: 0 },
      ],
    };
    render(<ContextBar breakdown={partialCapacityBreakdown} />);

    // The one measured component keeps its true capacity share.
    expect(screen.getByTestId('context-segment-history').style.width).toBe('25%');
    expect(screen.getByTestId('context-item-history')).toHaveTextContent('25.0%');

    // Unknown components get no segment and render as unknown with no share.
    expect(screen.queryByTestId('context-segment-system-prompt')).not.toBeInTheDocument();
    const systemItem = screen.getByTestId('context-item-system-prompt');
    expect(systemItem).toHaveTextContent('unknown');
    expect(systemItem).toHaveTextContent('—');
  });

  test('falls back to used-relative shares only when the payload carries no context limit (N14)', () => {
    const noLimitBreakdown = {
      model: 'gemini-3.5-flash',
      total_tokens: 400,
      percent_used: 0,
      components: [
        { name: 'system prompt', tokens: 300, share: 0.75 },
        { name: 'memory briefing', tokens: 0, share: 0 },
        { name: 'history', tokens: 100, share: 0.25 },
        { name: 'tool outputs', tokens: 0, share: 0 },
        { name: 'scratchpad', tokens: 0, share: 0 },
        { name: 'repo map', tokens: 0, share: 0 },
        { name: 'skill catalogue', tokens: 0, share: 0 },
      ],
    };
    render(<ContextBar breakdown={noLimitBreakdown} />);

    // Used-relative fallback: history is 100 of 400 used tokens.
    const historySegment = screen.getByTestId('context-segment-history');
    expect(historySegment.style.width).toBe('25%');
    expect(historySegment.title).toContain('of used tokens');

    // The fallback is explicit, and no denominator is fabricated.
    expect(screen.getByTestId('context-share-basis-note')).toBeInTheDocument();
    expect(screen.getByTestId('context-bar-panel')).not.toHaveTextContent('1,048,576');
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
