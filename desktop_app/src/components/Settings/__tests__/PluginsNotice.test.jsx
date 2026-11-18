import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import PluginsNotice from '../PluginsNotice';

describe('PluginsNotice', () => {
  it('states the tools route: tools arrive only via a bundled MCP server', () => {
    const { getByText, queryByText } = render(<PluginsNotice />);
    expect(getByText(/tools arrive only through an MCP server/i)).toBeInTheDocument();
    expect(getByText(/refused at install/i)).toBeInTheDocument();
    // The old "declared but not yet callable" sentence is gone:
    // manifests declaring tools are refused at install now, so the
    // situation it described can no longer occur.
    expect(queryByText(/not yet callable/i)).not.toBeInTheDocument();
  });
});
