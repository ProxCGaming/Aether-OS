/** Graph neighbourhood: the entity picker is fed by the same fact
 * list the Memory panel uses, a pick asks the engine for that
 * entity's neighbourhood, neighbours re-centre on click, and an
 * unknown entity is an honest empty result — never an error. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import GraphNeighbourhood from '../GraphNeighbourhood';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const sendMessage = jest.fn();
  const mockWsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  useAetherSocket.mockReturnValue({ wsRef: mockWsRef, sendMessage, isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { sendMessage, simulateMessage };
}

const FACTS = [
  { id: 'f1', entity_name: 'Maa', entity_type: 'person', text: 'Maa lives in North Kolkata.' },
  { id: 'f2', entity_name: 'Maa', entity_type: 'person', text: 'Maa called on Sunday.' },
  { id: 'f3', entity_name: 'North Kolkata', entity_type: 'place', text: 'Home is North Kolkata.' },
  { id: 'f4', entity_name: 'College PTM', entity_type: 'event', text: 'PTM on 14 Nov.' },
];

function renderWithFacts(props = {}) {
  const harness = makeHarness();
  render(<GraphNeighbourhood {...props} />);
  harness.simulateMessage({ type: 'MEMORY_GRAPH_RESPONSE', payload: { facts: FACTS } });
  return harness;
}

const MAA_HOOD = {
  centre: { entity_name: 'Maa', entity_type: 'person', fact_count: 18, episode_count: 7 },
  neighbours: [
    { entity_name: 'Arindam Roy', entity_type: 'person', cooccurrence_count: 23, fact_count: 31 },
    { entity_name: 'North Kolkata', entity_type: 'place', cooccurrence_count: 9, fact_count: 12 },
  ],
  replaced_chains: [
    { old_entity: 'Barasat', new_entity: 'North Kolkata', fact_id: 'f9' },
  ],
};

describe('GraphNeighbourhood', () => {
  test('requests the fact list on mount and shows a loading state until it answers', () => {
    const { sendMessage } = makeHarness();
    render(<GraphNeighbourhood />);
    expect(sendMessage).toHaveBeenCalledWith(
      'MEMORY_GRAPH_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(screen.getByTestId('graph-entities-load-state')).toHaveAttribute('data-load-display', 'loading');
  });

  test('the picker lists distinct entities with type and fact counts', () => {
    renderWithFacts();
    expect(screen.getByTestId('graph-entity-option-Maa')).toHaveTextContent('person');
    expect(screen.getByTestId('graph-entity-option-Maa')).toHaveTextContent('2 facts');
    expect(screen.getByTestId('graph-entity-option-North Kolkata')).toHaveTextContent('1 facts');
    expect(screen.getByTestId('graph-entity-option-College PTM')).toBeInTheDocument();
  });

  test('the picker search filters entities by name', () => {
    renderWithFacts();
    fireEvent.change(screen.getByTestId('graph-entity-search'), { target: { value: 'kolkata' } });
    expect(screen.getByTestId('graph-entity-option-North Kolkata')).toBeInTheDocument();
    expect(screen.queryByTestId('graph-entity-option-Maa')).not.toBeInTheDocument();
  });

  test('picking an entity asks for its neighbourhood at depth 1 and renders centre + neighbours', () => {
    const { sendMessage, simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    const ask = sendMessage.mock.calls.find((c) => c[0] === 'MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST');
    expect(ask).toBeTruthy();
    expect(ask[1].payload).toEqual({ entity_name: 'Maa', depth: 1 });
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    const centre = screen.getByTestId('graph-centre-card');
    expect(centre).toHaveTextContent('Maa');
    expect(centre).toHaveTextContent('18 facts');
    expect(centre).toHaveTextContent('7 episodes');
    expect(screen.getByTestId('graph-neighbour-Arindam Roy')).toHaveTextContent('×23 together');
    expect(screen.getByTestId('graph-neighbour-North Kolkata')).toHaveTextContent('×9 together');
  });

  test('the depth switch re-asks for the current centre at the new depth', () => {
    const { sendMessage, simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    sendMessage.mockClear();
    fireEvent.click(screen.getByTestId('graph-depth-2'));
    const ask = sendMessage.mock.calls.find((c) => c[0] === 'MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST');
    expect(ask).toBeTruthy();
    expect(ask[1].payload).toEqual({ entity_name: 'Maa', depth: 2 });
  });

  test('clicking a neighbour re-centres the neighbourhood on it', () => {
    const { sendMessage, simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    sendMessage.mockClear();
    fireEvent.click(screen.getByTestId('graph-neighbour-North Kolkata'));
    const ask = sendMessage.mock.calls.find((c) => c[0] === 'MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST');
    expect(ask).toBeTruthy();
    expect(ask[1].payload).toEqual({ entity_name: 'North Kolkata', depth: 1 });
  });

  test('replaced chains render as old → replaced by → new, and the names re-centre', () => {
    const { sendMessage, simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    const chains = screen.getByTestId('graph-replaced-chains');
    expect(chains).toHaveTextContent('Barasat');
    expect(chains).toHaveTextContent('was replaced by');
    expect(chains).toHaveTextContent('North Kolkata');
    sendMessage.mockClear();
    fireEvent.click(screen.getByTestId('graph-chain-old-0'));
    const ask = sendMessage.mock.calls.find((c) => c[0] === 'MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST');
    expect(ask[1].payload).toEqual({ entity_name: 'Barasat', depth: 1 });
  });

  test('an unknown entity renders an empty state naming it, not an error', () => {
    const { simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({
      type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE',
      payload: {
        centre: { entity_name: 'Maa', entity_type: null, fact_count: 0, episode_count: 0 },
        neighbours: [],
        replaced_chains: [],
      },
    });
    expect(screen.getByTestId('graph-unknown-entity')).toHaveTextContent('Maa');
    expect(screen.queryByTestId('graph-centre-card')).not.toBeInTheDocument();
    expect(screen.queryByTestId('graph-hood-load-state')).not.toBeInTheDocument();
  });

  test('a failed neighbourhood reply is an error state', () => {
    const { simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({
      type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE',
      payload: { success: false, error: 'graph store gone' },
    });
    const row = screen.getByTestId('graph-hood-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('graph store gone');
  });

  test('a known centre with no neighbours says so plainly', () => {
    const { simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-College PTM'));
    simulateMessage({
      type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE',
      payload: {
        centre: { entity_name: 'College PTM', entity_type: 'event', fact_count: 2, episode_count: 1 },
        neighbours: [],
        replaced_chains: [],
      },
    });
    expect(screen.getByTestId('graph-centre-card')).toHaveTextContent('College PTM');
    expect(screen.getByText(/No other entities appear together with College PTM yet/)).toBeInTheDocument();
  });

  test('the View facts action appears only when onOpenEntity is given, and reports the centre', () => {
    const onOpenEntity = jest.fn();
    const { simulateMessage } = renderWithFacts({ onOpenEntity });
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    fireEvent.click(screen.getByTestId('graph-centre-facts'));
    expect(onOpenEntity).toHaveBeenCalledWith('Maa');
  });

  test('without onOpenEntity there is no View facts action', () => {
    const { simulateMessage } = renderWithFacts();
    fireEvent.click(screen.getByTestId('graph-entity-option-Maa'));
    simulateMessage({ type: 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE', payload: MAA_HOOD });
    expect(screen.queryByTestId('graph-centre-facts')).not.toBeInTheDocument();
  });

  test('a passed-in entity list skips the fetch and feeds the picker', () => {
    const { sendMessage } = makeHarness();
    render(
      <GraphNeighbourhood
        entities={[{ entity_name: 'Maa', entity_type: 'person', fact_count: 18 }]}
      />,
    );
    expect(sendMessage.mock.calls.some((c) => c[0] === 'MEMORY_GRAPH_REQUEST')).toBe(false);
    expect(screen.getByTestId('graph-entity-option-Maa')).toHaveTextContent('18 facts');
  });

  test('initialEntity opens its neighbourhood on mount', () => {
    const { sendMessage } = makeHarness();
    render(<GraphNeighbourhood initialEntity="Maa" />);
    const ask = sendMessage.mock.calls.find((c) => c[0] === 'MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST');
    expect(ask).toBeTruthy();
    expect(ask[1].payload).toEqual({ entity_name: 'Maa', depth: 1 });
  });
});
