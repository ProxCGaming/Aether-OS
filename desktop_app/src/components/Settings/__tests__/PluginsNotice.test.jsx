import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import PluginsNotice from '../PluginsNotice';

describe('PluginsNotice', () => {
  it('renders correctly', () => {
    const { getByText } = render(<PluginsNotice />);
    expect(getByText(/not yet callable/i)).toBeInTheDocument();
  });
});
