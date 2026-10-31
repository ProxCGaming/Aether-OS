import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import RewindConfirmModal from '../components/RewindConfirmModal';

// Epic 8, E8-1.1b: the rewind confirmation modal. Rewind is
// state-only (grill Q2): the modal's job is to enumerate every file
// written after the chosen point and state plainly that those files
// will NOT be reverted, before the user commits to the rollback.
describe('RewindConfirmModal (E8-1.1b)', () => {
  const entry = {
    step_index: 1,
    checkpoint_id: 'cp0002abcd',
    label: 'After planner',
    files_written: [],
    timestamp: 101,
  };

  it('enumerates files written after the point as will NOT be reverted', () => {
    render(
      <RewindConfirmModal
        entry={entry}
        filesNotReverted={['notes/out.txt', 'src/app.py']}
        onConfirm={jest.fn()}
        onCancel={jest.fn()}
      />
    );
    const modal = screen.getByTestId('rewind-confirm-modal');
    expect(modal).toHaveTextContent('After planner');
    expect(modal).toHaveTextContent('will NOT be reverted');
    const list = screen.getByTestId('rewind-files-not-reverted');
    expect(list).toHaveTextContent('notes/out.txt');
    expect(list).toHaveTextContent('src/app.py');
  });

  it('says plainly when no files were written after the point', () => {
    render(
      <RewindConfirmModal
        entry={entry}
        filesNotReverted={[]}
        onConfirm={jest.fn()}
        onCancel={jest.fn()}
      />
    );
    expect(screen.queryByTestId('rewind-files-not-reverted')).not.toBeInTheDocument();
    expect(screen.getByTestId('rewind-confirm-modal')).toHaveTextContent(
      'No files were written after this point');
  });

  it('confirm and cancel fire their callbacks', () => {
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(
      <RewindConfirmModal
        entry={entry}
        filesNotReverted={['notes/out.txt']}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByTestId('rewind-confirm-btn'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('rewind-cancel-btn'));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('renders nothing without an entry', () => {
    const { container } = render(
      <RewindConfirmModal
        entry={null}
        filesNotReverted={[]}
        onConfirm={jest.fn()}
        onCancel={jest.fn()}
      />
    );
    expect(container).toBeEmptyDOMElement();
  });
});
