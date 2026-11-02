import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import GlobalArtifactsView from '../components/GlobalArtifactsView';

// react-markdown is ESM-only; stub it for Jest (same as the sibling
// DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; the chat window calls it on new messages.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function renderChat(props = {}) {
  const handlers = {
    onSendMessage: jest.fn(),
    onStopTask: jest.fn(),
    onApproveTool: jest.fn(),
    onApprovePlugin: jest.fn(),
    onChangeModel: jest.fn(),
    onDetach: jest.fn(),
    ...props.handlers,
  };
  delete props.handlers;
  const utils = render(
    <DraggableChatWindow
      messages={[]}
      status="Online"
      isThinking={false}
      providers={[]}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...handlers}
      {...props}
    />
  );
  return { ...utils, handlers };
}

// N08 (Epic 8 Astra review): "jump to conversation" could not identify
// the exact message — chat message wrappers carried no data-message-id
// anchor, and media navigation was funnelled through the artifact
// handler (artifact id space, canvas selection, ARTIFACT_GET_REQUEST).
describe('N08 — message anchors and media navigation split', () => {
  test('chat message wrappers expose data-message-id anchors', () => {
    const { container } = renderChat({
      messages: [
        { id: 'u-1', role: 'user', content: 'please draw a diagram' },
        { id: 'a-1', role: 'assistant', content: 'Done — diagram written.', isFinal: true },
      ],
    });

    expect(container.querySelector('[data-message-id="u-1"]')).not.toBeNull();
    expect(container.querySelector('[data-message-id="a-1"]')).not.toBeNull();
  });

  test('media jump routes to the media navigation handler, not the artifact handler', () => {
    const handlers = [];
    const mockWsRef = {
      current: {
        readyState: 1,
        send: jest.fn(),
        addEventListener: jest.fn((event, handler) => {
          if (event === 'message') handlers.push(handler);
        }),
        removeEventListener: jest.fn(),
      },
    };
    const onNavigateToSession = jest.fn();
    const onNavigateToMedia = jest.fn();
    render(
      <GlobalArtifactsView
        wsRef={mockWsRef}
        sessions={[{ id: 'sess-1', title: 'UX Research' }]}
        onNavigateToSession={onNavigateToSession}
        onNavigateToMedia={onNavigateToMedia}
      />
    );

    fireEvent.click(screen.getByTestId('section-tab-media'));
    expect(screen.getByTestId('media-library-view')).toBeInTheDocument();

    const mediaItem = {
      id: 'med-1',
      session_id: 'sess-1',
      message_id: 'msg-1',
      collection: 'generated',
      media_type: 'image',
      file_path: '/home/user/workspace/diagram.png',
      file_size: 85000,
      mime_type: 'image/png',
      status: 'analyzed',
      probe_metadata: { origin: 'agent_generated', tool: 'write_file' },
      origin: 'agent_generated',
      tags: ['diagram'],
      user_notes: '',
      created_at: 3000,
      file_exists: true,
    };
    act(() => {
      handlers.forEach((h) =>
        h({
          data: JSON.stringify({
            type: 'MEDIA_LIST_RESPONSE',
            payload: { media: [mediaItem], total: 1 },
          }),
        })
      );
    });

    fireEvent.click(screen.getByTestId('jump-to-conversation-med-1'));

    expect(onNavigateToMedia).toHaveBeenCalledTimes(1);
    expect(onNavigateToMedia).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'med-1',
        session_id: 'sess-1',
        message_id: 'msg-1',
      })
    );
    expect(onNavigateToSession).not.toHaveBeenCalled();
  });
});
