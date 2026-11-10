import React from 'react';
import { Scissors, Copy, Clipboard, CheckCheck } from 'lucide-react';

export default function ContextMenu({ menu, onClose, onShowToast }) {
  if (!menu) return null;

  // D1-C3: Cut/Paste commit into the element the menu opened on.
  // Writing through the prototype value setter and dispatching
  // an `input` event routes the new value through the onChange
  // of whichever React component owns THAT field — the composer,
  // the approval-args editor, any field. The menu used to call
  // the composer's own setInput unconditionally, so cutting
  // text in the args editor overwrote the composer draft and
  // left the editor itself untouched.
  const commitElementValue = (el, nextVal) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) {
      setter.call(el, nextVal);
    } else {
      el.value = nextVal;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
  };

  const handleCopySelection = async (e) => {
    e.stopPropagation();
    if (menu.selectedText) {
      try {
        await navigator.clipboard.writeText(menu.selectedText);
        onShowToast('Copied selection to clipboard');
      } catch (err) {
        console.error(err);
      }
    }
    onClose();
  };

  const handleCopyFull = async (e) => {
    e.stopPropagation();
    if (menu.fullText) {
      try {
        await navigator.clipboard.writeText(menu.fullText);
        onShowToast(menu.isThought ? 'Copied thought to clipboard' : 'Copied message to clipboard');
      } catch (err) {
        console.error(err);
      }
    }
    onClose();
  };

  const handleCut = async (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      const el = menu.inputElement;
      const start = el.selectionStart || 0;
      const end = el.selectionEnd || 0;
      const val = el.value || '';
      const cutText = val.slice(start, end);
      if (cutText) {
        try {
          await navigator.clipboard.writeText(cutText);
          const nextVal = val.slice(0, start) + val.slice(end);
          commitElementValue(el, nextVal);
          onShowToast('Cut to clipboard');
          setTimeout(() => {
            el.selectionStart = el.selectionEnd = start;
            el.focus();
          }, 0);
        } catch (err) {
          console.error(err);
        }
      }
    }
    onClose();
  };

  const handlePaste = async (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      const el = menu.inputElement;
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          const start = el.selectionStart || 0;
          const end = el.selectionEnd || 0;
          const val = el.value || '';
          const nextVal = val.slice(0, start) + text + val.slice(end);
          commitElementValue(el, nextVal);
          onShowToast('Pasted from clipboard');
          setTimeout(() => {
            el.selectionStart = el.selectionEnd = start + text.length;
            el.focus();
          }, 0);
        }
      } catch (err) {
        console.error('Clipboard paste failed:', err);
      }
    }
    onClose();
  };

  const handleSelectAll = (e) => {
    e.stopPropagation();
    if (menu.inputElement) {
      menu.inputElement.select();
      menu.inputElement.focus();
    } else if (menu.targetElement) {
      const range = document.createRange();
      range.selectNodeContents(menu.targetElement);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    onClose();
  };

  return (
    <div 
      className="ios-context-menu"
      style={{ left: `${menu.x}px`, top: `${menu.y}px` }}
      onClick={(e) => e.stopPropagation()}
    >
      {menu.isInput ? (
        <>
          <div 
            className={`ios-context-item ${!menu.selectedText ? 'disabled' : ''}`}
            onClick={handleCut}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Scissors size={14} color="#F5B942" />
              <span>Cut</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+X</span>
          </div>

          <div 
            className={`ios-context-item ${!menu.selectedText ? 'disabled' : ''}`}
            onClick={handleCopySelection}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Copy size={14} color="#F5B942" />
              <span>Copy</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+C</span>
          </div>

          <div className="ios-context-item" onClick={handlePaste}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Clipboard size={14} color="#F5B942" />
              <span>Paste</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+V</span>
          </div>

          <div className="ios-context-divider" />

          <div className="ios-context-item" onClick={handleSelectAll}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <CheckCheck size={14} color="#F5B942" />
              <span>Select All</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+A</span>
          </div>
        </>
      ) : (
        <>
          {menu.selectedText ? (
            <div className="ios-context-item" onClick={handleCopySelection}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Copy size={14} color="#F5B942" />
                <span>Copy Selection</span>
              </div>
              <span className="ios-context-shortcut">Ctrl+C</span>
            </div>
          ) : null}

          {menu.fullText ? (
            <div className="ios-context-item" onClick={handleCopyFull}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Copy size={14} color="#F5B942" />
                <span>{menu.isThought ? 'Copy Thought' : 'Copy Message'}</span>
              </div>
            </div>
          ) : null}

          <div className="ios-context-divider" />

          <div className="ios-context-item" onClick={handleSelectAll}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <CheckCheck size={14} color="#F5B942" />
              <span>Select All</span>
            </div>
            <span className="ios-context-shortcut">Ctrl+A</span>
          </div>
        </>
      )}
    </div>
  );
}
