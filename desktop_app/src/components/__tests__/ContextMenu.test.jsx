import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ContextMenu from '../ContextMenu';
import { buildContextMenuState } from '../contextMenuState';

// Wave 5 (Annex C row 6, menu-system seam): the context-menu
// machinery extracted from DraggableChatWindow — the pure menu
// model builder plus the menu component — pinned here directly.

function setClipboard(impl) {
  Object.defineProperty(window.navigator, 'clipboard', {
    value: impl,
    configurable: true,
  });
}

function stubSelection(text) {
  const original = window.getSelection;
  window.getSelection = () => ({ toString: () => text });
  return () => { window.getSelection = original; };
}

describe('buildContextMenuState', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  test('returns null when there is no selection, no input and no message text', () => {
    const restore = stubSelection('');
    const target = document.createElement('div');
    document.body.appendChild(target);
    const menu = buildContextMenuState({ target, clientX: 10, clientY: 10 }, null);
    expect(menu).toBeNull();
    restore();
  });

  test('input targets produce an input menu bound to the element', () => {
    const restore = stubSelection('');
    const target = document.createElement('textarea');
    document.body.appendChild(target);
    const menu = buildContextMenuState({ target, clientX: 10, clientY: 10 }, null);
    expect(menu.isInput).toBe(true);
    expect(menu.inputElement).toBe(target);
    expect(menu.x).toBe(10);
    expect(menu.y).toBe(10);
    restore();
  });

  test('bubble targets carry the bubble text; thought targets are flagged', () => {
    const restore = stubSelection('');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    Object.defineProperty(bubble, 'innerText', { value: 'bubble text', configurable: true });
    const inner = document.createElement('span');
    bubble.appendChild(inner);
    document.body.appendChild(bubble);
    const menu = buildContextMenuState({ target: inner, clientX: 5, clientY: 5 }, null);
    expect(menu.fullText).toBe('bubble text');
    expect(menu.isThought).toBe(false);
    expect(menu.targetElement).toBe(bubble);

    const thought = document.createElement('div');
    thought.className = 'thought-step-content';
    Object.defineProperty(thought, 'innerText', { value: 'thought text', configurable: true });
    const tInner = document.createElement('span');
    thought.appendChild(tInner);
    document.body.appendChild(thought);
    const tMenu = buildContextMenuState({ target: tInner, clientX: 5, clientY: 5 }, null);
    expect(tMenu.fullText).toBe('thought text');
    expect(tMenu.isThought).toBe(true);
    restore();
  });

  test('a live selection is captured trimmed', () => {
    const restore = stubSelection('  chosen words  ');
    const target = document.createElement('div');
    document.body.appendChild(target);
    const menu = buildContextMenuState({ target, clientX: 1, clientY: 2 }, null);
    expect(menu.selectedText).toBe('chosen words');
    restore();
  });

  test('coordinates are made container-relative and clamped inside it', () => {
    const restore = stubSelection('');
    const target = document.createElement('textarea');
    document.body.appendChild(target);
    const container = {
      getBoundingClientRect: () => ({ left: 100, top: 50, width: 300, height: 200 }),
    };
    // 100 (rel) + 190 (menu width) > 300 - 10  =>  x = 300 - 190 - 10 = 100
    // 100 (rel) + 180 (input menu height) > 200 - 10  =>  y = 200 - 180 - 10 = 10
    const menu = buildContextMenuState({ target, clientX: 200, clientY: 150 }, container);
    expect(menu.x).toBe(100);
    expect(menu.y).toBe(10);
    restore();
  });
});

describe('ContextMenu', () => {
  test('renders nothing without a menu', () => {
    const { container } = render(<ContextMenu menu={null} onClose={jest.fn()} onShowToast={jest.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  test('input menu offers Cut/Copy/Paste/Select All with shortcuts', () => {
    const menu = { x: 0, y: 0, isInput: true, selectedText: 'abc', fullText: '', isThought: false, inputElement: null, targetElement: null };
    render(<ContextMenu menu={menu} onClose={jest.fn()} onShowToast={jest.fn()} />);
    expect(screen.getByText('Cut')).toBeInTheDocument();
    expect(screen.getByText('Copy')).toBeInTheDocument();
    expect(screen.getByText('Paste')).toBeInTheDocument();
    expect(screen.getByText('Select All')).toBeInTheDocument();
    expect(screen.getByText('Ctrl+X')).toBeInTheDocument();
  });

  test('copy message writes the full text and toasts', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    setClipboard({ writeText, readText: jest.fn() });
    const onClose = jest.fn();
    const onShowToast = jest.fn();
    const menu = { x: 0, y: 0, isInput: false, selectedText: '', fullText: 'whole message', isThought: false, inputElement: null, targetElement: null };
    render(<ContextMenu menu={menu} onClose={onClose} onShowToast={onShowToast} />);
    fireEvent.click(screen.getByText('Copy Message'));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('whole message');
    expect(onShowToast).toHaveBeenCalledWith('Copied message to clipboard');
    expect(onClose).toHaveBeenCalled();
  });

  test('thought menus label the full copy as Copy Thought', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    setClipboard({ writeText, readText: jest.fn() });
    const onShowToast = jest.fn();
    const menu = { x: 0, y: 0, isInput: false, selectedText: '', fullText: 'a thought', isThought: true, inputElement: null, targetElement: null };
    render(<ContextMenu menu={menu} onClose={jest.fn()} onShowToast={onShowToast} />);
    fireEvent.click(screen.getByText('Copy Thought'));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('a thought');
    expect(onShowToast).toHaveBeenCalledWith('Copied thought to clipboard');
  });

  test('D1-C3: paste commits into the element the menu opened on', async () => {
    const readText = jest.fn().mockResolvedValue('pasted');
    setClipboard({ writeText: jest.fn().mockResolvedValue(undefined), readText });
    const onShowToast = jest.fn();
    const onClose = jest.fn();
    const textarea = document.createElement('textarea');
    textarea.value = 'ab';
    document.body.appendChild(textarea);
    textarea.selectionStart = 1;
    textarea.selectionEnd = 1;
    const menu = { x: 0, y: 0, isInput: true, selectedText: '', fullText: '', isThought: false, inputElement: textarea, targetElement: textarea };
    render(<ContextMenu menu={menu} onClose={onClose} onShowToast={onShowToast} />);
    fireEvent.click(screen.getByText('Paste'));
    await Promise.resolve();
    await Promise.resolve();
    expect(textarea.value).toBe('apastedb');
    expect(onShowToast).toHaveBeenCalledWith('Pasted from clipboard');
    expect(onClose).toHaveBeenCalled();
    document.body.removeChild(textarea);
  });

  test('D1-C3: cut removes the selection from that same element', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    setClipboard({ writeText, readText: jest.fn() });
    const onShowToast = jest.fn();
    const textarea = document.createElement('textarea');
    textarea.value = 'hello world';
    document.body.appendChild(textarea);
    textarea.selectionStart = 0;
    textarea.selectionEnd = 5;
    const menu = { x: 0, y: 0, isInput: true, selectedText: 'hello', fullText: '', isThought: false, inputElement: textarea, targetElement: textarea };
    render(<ContextMenu menu={menu} onClose={jest.fn()} onShowToast={onShowToast} />);
    fireEvent.click(screen.getByText('Cut'));
    await Promise.resolve();
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('hello');
    expect(textarea.value).toBe(' world');
    expect(onShowToast).toHaveBeenCalledWith('Cut to clipboard');
    document.body.removeChild(textarea);
  });
});
