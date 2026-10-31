import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import SnipOverlay from '../components/SnipOverlay';
import SettingsPanel from '../SettingsPanel';

// Stub react-markdown
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn()
  })
}));

Element.prototype.scrollIntoView = jest.fn();

describe('E8-4.1: Snip Flow (Composer attachment & capture overlay)', () => {
  describe('Composer Snip & ask button and attachment UI in DraggableChatWindow', () => {
    it('renders the Snip & ask button in the composer', () => {
      render(
        <DraggableChatWindow
          messages={[]}
          status="Online"
          onSendMessage={jest.fn()}
        />
      );

      const snipBtn = screen.getByTestId('snip-ask-btn');
      expect(snipBtn).toBeInTheDocument();
      expect(snipBtn).toHaveTextContent(/snip/i);
    });

    it('triggers snip overlay when Snip & ask button is clicked', () => {
      const onTriggerSnip = jest.fn();
      render(
        <DraggableChatWindow
          messages={[]}
          status="Online"
          onSendMessage={jest.fn()}
          onTriggerSnip={onTriggerSnip}
        />
      );

      const snipBtn = screen.getByTestId('snip-ask-btn');
      fireEvent.click(snipBtn);
      expect(onTriggerSnip).toHaveBeenCalledTimes(1);
    });

    it('displays attached thumbnail in composer with a working removal button', () => {
      const onSendMessage = jest.fn();
      const initialAttachments = [
        {
          name: 'snip-1.png',
          mime_type: 'image/png',
          data: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
        },
      ];

      render(
        <DraggableChatWindow
          messages={[]}
          status="Online"
          onSendMessage={onSendMessage}
          initialAttachments={initialAttachments}
        />
      );

      // Verify composer attachment thumbnail is visible
      const thumb = screen.getByTestId('composer-attachment-thumbnail');
      expect(thumb).toBeInTheDocument();
      expect(thumb).toHaveAttribute('src', initialAttachments[0].data);

      // Verify removal button
      const removeBtn = screen.getByTestId('remove-attachment-btn');
      expect(removeBtn).toBeInTheDocument();

      // Click removal button
      fireEvent.click(removeBtn);
      expect(screen.queryByTestId('composer-attachment-thumbnail')).not.toBeInTheDocument();
    });

    it('submits text and attachments together on send', () => {
      const onSendMessage = jest.fn();
      const snipData = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

      render(
        <DraggableChatWindow
          messages={[]}
          status="Online"
          onSendMessage={onSendMessage}
          initialAttachments={[{ name: 'snip.png', mime_type: 'image/png', data: snipData }]}
        />
      );

      const input = screen.getByPlaceholderText(/message aether/i);
      fireEvent.change(input, { target: { value: 'What is shown in this capture?' } });

      const form = input.closest('form');
      fireEvent.submit(form);

      expect(onSendMessage).toHaveBeenCalledWith(
        'What is shown in this capture?',
        expect.arrayContaining([
          expect.objectContaining({ name: 'snip.png', data: snipData }),
        ])
      );
    });

    it('renders image attachment in user message bubble', () => {
      const snipData = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
      const messages = [
        {
          id: 'msg-user-1',
          role: 'user',
          content: 'Here is the captured snip',
          isFinal: true,
          attachments: [
            {
              name: 'snip.png',
              mime_type: 'image/png',
              data: snipData,
            },
          ],
        },
      ];

      render(
        <DraggableChatWindow
          messages={messages}
          status="Online"
          onSendMessage={jest.fn()}
        />
      );

      const msgAttachments = screen.getByTestId('message-attachments');
      expect(msgAttachments).toBeInTheDocument();
      const img = msgAttachments.querySelector('img');
      expect(img).toBeInTheDocument();
      expect(img).toHaveAttribute('src', snipData);
    });
  });

  describe('SnipOverlay drag-to-select capture component', () => {
    it('renders overlay and handles drag selection to capture region', () => {
      const onCapture = jest.fn();
      const onCancel = jest.fn();

      render(
        <SnipOverlay
          isOpen={true}
          onCapture={onCapture}
          onCancel={onCancel}
        />
      );

      const overlay = screen.getByTestId('snip-overlay');
      expect(overlay).toBeInTheDocument();

      // Drag select a rectangle from (100, 100) to (300, 250)
      fireEvent.mouseDown(overlay, { clientX: 100, clientY: 100 });
      fireEvent.mouseMove(overlay, { clientX: 300, clientY: 250 });

      // Selection box should be rendered
      const box = screen.getByTestId('snip-selection-box');
      expect(box).toBeInTheDocument();

      // Mouse up to complete selection
      fireEvent.mouseUp(overlay, { clientX: 300, clientY: 250 });

      expect(onCapture).toHaveBeenCalledTimes(1);
      const callArg = onCapture.mock.calls[0][0];
      expect(callArg).toMatchObject({
        name: expect.stringMatching(/snip.*\.png/),
        mime_type: 'image/png',
        data: expect.stringMatching(/^data:image\/png;base64,/),
        bounds: expect.objectContaining({ width: 200, height: 150 }),
      });
    });

    it('cancels on Escape key or cancel button', () => {
      const onCapture = jest.fn();
      const onCancel = jest.fn();

      render(
        <SnipOverlay
          isOpen={true}
          onCapture={onCapture}
          onCancel={onCancel}
        />
      );

      const cancelBtn = screen.getByTestId('snip-cancel-btn');
      fireEvent.click(cancelBtn);
      expect(onCancel).toHaveBeenCalledTimes(1);

      // Also on Escape keydown
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(onCancel).toHaveBeenCalledTimes(2);
    });
  });

  describe('SettingsPanel snip hotkey configuration', () => {
    function makeWsRef() {
      let messageHandler = null;
      const wsRef = {
        current: {
          readyState: 1,
          send: jest.fn(),
          addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
          removeEventListener: jest.fn(),
        },
      };
      return { wsRef, getHandler: () => messageHandler };
    }

    it('allows viewing and updating screen snip hotkey in Advanced settings', () => {
      const registerShortcutMock = jest.fn();
      window.electronAPI = {
        registerSnipShortcut: registerShortcutMock,
      };

      const { wsRef, getHandler } = makeWsRef();
      render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

      // Switch to Advanced tab
      fireEvent.click(screen.getByText('Advanced'));

      // Check input presence
      const input = screen.getByTestId('snip-hotkey-input');
      expect(input).toBeInTheDocument();
      expect(input.value).toBe('CommandOrControl+Shift+S');

      // Update hotkey
      fireEvent.change(input, { target: { value: 'CommandOrControl+Alt+S' } });
      fireEvent.blur(input);

      expect(registerShortcutMock).toHaveBeenCalledWith('CommandOrControl+Alt+S');
      const updateCall = wsRef.current.send.mock.calls.find(c => c[0].includes('CONFIG_UPDATE_REQUEST'));
      expect(updateCall).toBeDefined();
      expect(JSON.parse(updateCall[0])).toMatchObject({
        type: 'CONFIG_UPDATE_REQUEST',
        payload: { config: { snip_hotkey: 'CommandOrControl+Alt+S' } },
      });

      // Verify incoming CONFIG_GET_RESPONSE updates the state
      act(() => {
        getHandler()({
          data: JSON.stringify({
            type: 'CONFIG_GET_RESPONSE',
            payload: {
              config: {
                snip_hotkey: 'Ctrl+Shift+X',
              },
            },
          }),
        });
      });

      expect(input.value).toBe('Ctrl+Shift+X');
    });
  });
});
