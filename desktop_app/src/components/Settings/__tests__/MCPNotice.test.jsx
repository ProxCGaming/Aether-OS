import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import MCPNotice from '../MCPNotice';

describe('MCPNotice', () => {
  it('renders correctly', () => {
    const { getByText } = render(<MCPNotice />);
    expect(getByText(/display-only mode/i)).toBeInTheDocument();
  });
});
