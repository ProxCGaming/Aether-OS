import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DataRenderer from '../DataRenderer';

// DataRenderer: structured artifact content — JSON as a collapsible
// tree of labelled values, CSV as a table. Pins the two primary
// content shapes plus the honest invalid-JSON fallback.

describe('DataRenderer', () => {
  describe('JSON tree', () => {
    it('renders keys and scalar values of a JSON object', () => {
      const jsonStr = JSON.stringify({ name: 'Aether', version: 1, active: true });
      render(<DataRenderer content={jsonStr} format="json" />);
      expect(screen.getByTestId('data-renderer')).toBeInTheDocument();
      expect(screen.getByTestId('json-tree')).toBeInTheDocument();
      expect(screen.getByText('name:')).toBeInTheDocument();
      expect(screen.getByText('"Aether"')).toBeInTheDocument();
      expect(screen.getByText('version:')).toBeInTheDocument();
      expect(screen.getByText('active:')).toBeInTheDocument();
      expect(screen.getByText('true')).toBeInTheDocument();
    });

    it('renders nested objects and null values', () => {
      const jsonStr = JSON.stringify({ nested: { inner: 'deep' }, missing: null });
      render(<DataRenderer content={jsonStr} format="json" />);
      expect(screen.getByText('nested:')).toBeInTheDocument();
      expect(screen.getByText('inner:')).toBeInTheDocument();
      expect(screen.getByText('"deep"')).toBeInTheDocument();
      expect(screen.getByText('missing:')).toBeInTheDocument();
      expect(screen.getByText('null')).toBeInTheDocument();
    });

    it('detects JSON from the content when no format is given', () => {
      const jsonStr = JSON.stringify({ detected: 'yes' });
      render(<DataRenderer content={jsonStr} />);
      expect(screen.getByTestId('json-tree')).toBeInTheDocument();
      expect(screen.getByText('detected:')).toBeInTheDocument();
    });

    it('collapses and re-expands a node when its header is clicked', () => {
      const jsonStr = JSON.stringify({ name: 'Aether', version: 1 });
      render(<DataRenderer content={jsonStr} format="json" />);
      expect(screen.getByText('name:')).toBeInTheDocument();

      fireEvent.click(screen.getByText(/2 items/));
      expect(screen.queryByText('name:')).toBeNull();
      expect(screen.queryByText('"Aether"')).toBeNull();

      fireEvent.click(screen.getByText(/2 items/));
      expect(screen.getByText('name:')).toBeInTheDocument();
      expect(screen.getByText('"Aether"')).toBeInTheDocument();
    });

    it('shows the raw content with an invalid-JSON notice for broken JSON', () => {
      render(<DataRenderer content="{not valid json" format="json" />);
      expect(screen.getByText(/Invalid JSON/)).toBeInTheDocument();
      expect(screen.getByText(/not valid json/)).toBeInTheDocument();
      expect(screen.queryByTestId('json-tree')).toBeNull();
    });
  });

  describe('CSV table', () => {
    it('renders headers and body rows as a table', () => {
      const csvStr = 'Name,Age,Role\nAlice,30,Engineer\nBob,25,Designer';
      render(<DataRenderer content={csvStr} format="csv" />);
      expect(screen.getByTestId('csv-table')).toBeInTheDocument();
      expect(screen.getByText('Name')).toBeInTheDocument();
      expect(screen.getByText('Role')).toBeInTheDocument();
      expect(screen.getByText('Alice')).toBeInTheDocument();
      expect(screen.getByText('30')).toBeInTheDocument();
      expect(screen.getByText('Engineer')).toBeInTheDocument();
      expect(screen.getByText('Bob')).toBeInTheDocument();
      expect(screen.getByText('Designer')).toBeInTheDocument();
    });

    it('keeps a quoted cell containing a comma as one cell', () => {
      const csvStr = 'Name,Note\nAlice,"Hello, world"';
      render(<DataRenderer content={csvStr} format="csv" />);
      expect(screen.getByText('Hello, world')).toBeInTheDocument();
      expect(screen.queryByText('Hello')).toBeNull();
    });
  });
});
