import React from 'react';
import { render, screen } from '@testing-library/react';
import Dashboard from '../Dashboard';

describe('Dashboard Component', () => {
  it('renders initializing state correctly', () => {
    render(<Dashboard status="Initializing" activeTasks={0} pluginCount={0} memoryMb={null} />);
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Initializing')).toBeInTheDocument();
  });

  it('displays the correct number of active tasks', () => {
    render(<Dashboard status="Online" activeTasks={5} pluginCount={2} memoryMb={1024} />);
    expect(screen.getByText('5')).toBeInTheDocument();
  });
});
