import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import MediaGrid from '../MediaGrid';

// Wave 5 (Annex C row 10): the grid seam split out of MediaLibrary.
// These tests pin the grid's prop contract directly — MediaLibrary's
// own suite drives the same cards end-to-end through the socket.

const ITEM = {
  id: 'med-1',
  session_id: 'sess-1',
  message_id: 'msg-1',
  collection: 'user',
  media_type: 'image',
  file_path: '/home/user/screenshots/snip_login.png',
  file_size: 45000,
  origin: 'snip',
  tags: ['login', 'ui'],
  user_notes: 'Login screen capture',
  created_at: 1000,
  file_exists: true,
};

const gridProps = (overrides = {}) => ({
  items: [ITEM],
  getSessionTitle: (id) => (id === 'sess-1' ? 'UX Research' : id),
  failedThumbs: new Set(),
  onThumbError: jest.fn(),
  onOpenDetail: jest.fn(),
  onDeletePrompt: jest.fn(),
  onJumpToChat: jest.fn(),
  pendingDeleteFor: () => undefined,
  pendingMetaFor: () => undefined,
  getOriginLabel: () => 'Snip',
  getOriginBadgeStyle: () => ({}),
  ...overrides,
});

describe('MediaGrid — card rendering from props', () => {
  it('renders the thumbnail through the file:// seam, filename, origin and session', () => {
    render(<MediaGrid {...gridProps()} />);
    const thumb = screen.getByTestId('media-thumb-med-1');
    expect(thumb.tagName).toBe('IMG');
    expect(thumb.getAttribute('src')).toContain('snip_login.png');
    expect(screen.getByText('snip_login.png')).toBeInTheDocument();
    expect(screen.getByTestId('media-origin-med-1')).toHaveTextContent('Snip');
    expect(screen.getByTestId('media-session-med-1')).toHaveTextContent('UX Research');
    // 45000 bytes through the shared formatter (C2).
    expect(screen.getByText('43.9 KB')).toBeInTheDocument();
  });

  it('routes card, delete and jump clicks to the prop handlers', () => {
    // The library's real handlers stopPropagation on the nested
    // buttons (handleJumpToChat / handleDeletePrompt), so the card
    // click must not also fire for them — the mocks mirror that.
    const props = gridProps({
      onJumpToChat: jest.fn((item, e) => e.stopPropagation()),
      onDeletePrompt: jest.fn((item, e) => e.stopPropagation()),
    });
    render(<MediaGrid {...props} />);
    fireEvent.click(screen.getByTestId('jump-to-conversation-med-1'));
    expect(props.onJumpToChat).toHaveBeenCalledTimes(1);
    expect(props.onOpenDetail).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('media-delete-btn-med-1'));
    expect(props.onDeletePrompt).toHaveBeenCalledTimes(1);
    expect(props.onOpenDetail).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('media-card-med-1'));
    expect(props.onOpenDetail).toHaveBeenCalledWith(ITEM);
  });

  it('renders the missing-file badge instead of a thumbnail when file_exists is false', () => {
    render(<MediaGrid {...gridProps({ items: [{ ...ITEM, file_exists: false }] })} />);
    expect(screen.getByTestId('media-missing-badge-med-1')).toBeInTheDocument();
    expect(screen.queryByTestId('media-thumb-med-1')).not.toBeInTheDocument();
  });

  it('falls back to the icon placeholder and reports the failure when a thumbnail errors (D3-07)', () => {
    const props = gridProps();
    const { rerender } = render(<MediaGrid {...props} />);
    fireEvent.error(screen.getByTestId('media-thumb-med-1'));
    expect(props.onThumbError).toHaveBeenCalledWith('med-1');
    rerender(<MediaGrid {...props} failedThumbs={new Set(['med-1'])} />);
    expect(screen.queryByTestId('media-thumb-med-1')).not.toBeInTheDocument();
    expect(screen.getByText('Image')).toBeInTheDocument();
  });

  it('derives the queued markings from the pending* props (RUX-A-09)', () => {
    render(
      <MediaGrid
        {...gridProps({
          pendingDeleteFor: (id) => (id === 'med-1' ? { type: 'MEDIA_DELETE_REQUEST' } : undefined),
          pendingMetaFor: (id) => (id === 'med-1' ? { type: 'MEDIA_METADATA_UPDATE_REQUEST' } : undefined),
        })}
      />
    );
    expect(screen.getByTestId('media-delete-queued-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-meta-queued-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-delete-btn-med-1')).toBeDisabled();
  });
});
