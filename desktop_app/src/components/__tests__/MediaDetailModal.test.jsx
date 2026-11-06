import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import MediaDetailModal from '../MediaDetailModal';

// Wave 5 (Annex C row 10): the detail-modal seam split out of
// MediaLibrary. These tests pin the modal's prop contract — the
// library keeps the edit state and handlers; the modal renders and
// reports back through them.

const ITEM = {
  id: 'med-1',
  session_id: 'sess-1',
  collection: 'user',
  media_type: 'image',
  file_path: '/home/user/screenshots/snip_login.png',
  file_size: 45000,
  origin: 'snip',
  tags: ['login'],
  user_notes: 'Login screen capture',
  file_exists: true,
};

const modalProps = (overrides = {}) => ({
  item: ITEM,
  sessionTitle: 'UX Research',
  editTags: ['login', 'ui'],
  tagInput: '',
  editNotes: 'Login screen capture',
  thumbFailed: false,
  deleteQueued: false,
  metaQueued: false,
  getOriginLabel: () => 'Snip',
  getOriginBadgeStyle: () => ({}),
  onClose: jest.fn(),
  onSave: jest.fn(),
  onJumpToChat: jest.fn(),
  onAddTag: jest.fn(),
  onRemoveTag: jest.fn(),
  onTagInputChange: jest.fn(),
  onNotesChange: jest.fn(),
  onThumbError: jest.fn(),
  ...overrides,
});

describe('MediaDetailModal — presentation from props', () => {
  it('renders the preview, origin badge, size and session title', () => {
    render(<MediaDetailModal {...modalProps()} />);
    expect(screen.getByTestId('media-detail-modal')).toBeInTheDocument();
    const preview = screen.getByTestId('media-detail-preview');
    expect(preview.tagName).toBe('IMG');
    expect(preview.getAttribute('src')).toContain('snip_login.png');
    expect(screen.getByText('Snip')).toBeInTheDocument();
    expect(screen.getByText('43.9 KB')).toBeInTheDocument();
    expect(screen.getByText('UX Research')).toBeInTheDocument();
    expect(screen.getByText('snip_login.png')).toBeInTheDocument();
  });

  it('shows the missing-file alert and no preview when the file is gone', () => {
    render(<MediaDetailModal {...modalProps({ item: { ...ITEM, file_exists: false } })} />);
    expect(screen.queryByTestId('media-detail-preview')).not.toBeInTheDocument();
    expect(screen.getByText(/missing or was moved externally/)).toBeInTheDocument();
  });

  it('suppresses the preview when the thumbnail already failed (thumbFailed)', () => {
    render(<MediaDetailModal {...modalProps({ thumbFailed: true })} />);
    expect(screen.queryByTestId('media-detail-preview')).not.toBeInTheDocument();
  });
});

describe('MediaDetailModal — edit surface reports through handlers', () => {
  it('renders the edit tags and removes through onRemoveTag', () => {
    const props = modalProps();
    render(<MediaDetailModal {...props} />);
    const tagItems = screen.getAllByTestId('media-tag-item');
    expect(tagItems).toHaveLength(2);
    expect(tagItems[0]).toHaveTextContent('#login');
    fireEvent.click(tagItems[0].querySelector('button'));
    expect(props.onRemoveTag).toHaveBeenCalledWith('login');
  });

  it('reports tag input changes and adds on Enter or the Add button', () => {
    const props = modalProps({ tagInput: 'new-tag' });
    render(<MediaDetailModal {...props} />);
    const input = screen.getByTestId('media-tag-input');
    fireEvent.change(input, { target: { value: 'other' } });
    expect(props.onTagInputChange).toHaveBeenCalledWith('other');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(props.onAddTag).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('media-add-tag-btn'));
    expect(props.onAddTag).toHaveBeenCalledTimes(2);
  });

  it('reports notes edits and saves through onSave', () => {
    const props = modalProps();
    render(<MediaDetailModal {...props} />);
    fireEvent.change(screen.getByTestId('media-notes-textarea'), { target: { value: 'updated' } });
    expect(props.onNotesChange).toHaveBeenCalledWith('updated');
    fireEvent.click(screen.getByTestId('media-save-metadata-btn'));
    expect(props.onSave).toHaveBeenCalledTimes(1);
  });

  it('closes from the backdrop, the close button and Cancel — all through onClose', () => {
    const props = modalProps();
    render(<MediaDetailModal {...props} />);
    fireEvent.click(screen.getByTestId('media-modal-close-btn'));
    fireEvent.click(screen.getByText('Cancel'));
    fireEvent.click(screen.getByTestId('media-detail-modal'));
    expect(props.onClose).toHaveBeenCalledTimes(3);
  });

  it('jumps to the conversation through onJumpToChat with the item', () => {
    const props = modalProps();
    render(<MediaDetailModal {...props} />);
    fireEvent.click(screen.getByTestId('jump-to-conversation-med-1'));
    expect(props.onJumpToChat).toHaveBeenCalledTimes(1);
    expect(props.onJumpToChat.mock.calls[0][0]).toBe(ITEM);
  });
});

describe('MediaDetailModal — queued states (RUX-A-09)', () => {
  it('shows both queued notices and disables Save while an edit is queued', () => {
    render(<MediaDetailModal {...modalProps({ deleteQueued: true, metaQueued: true })} />);
    expect(screen.getByTestId('media-detail-delete-queued')).toBeInTheDocument();
    expect(screen.getByTestId('media-detail-meta-queued')).toBeInTheDocument();
    expect(screen.getByTestId('media-save-metadata-btn')).toBeDisabled();
  });
});
