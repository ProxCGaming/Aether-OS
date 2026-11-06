// Wave 5 (Annex C row 6, menu-system seam): build the menu model
// for a contextmenu event inside the chat window, or null when
// the event offers nothing the menu can act on (no selection,
// not an input, no thought/message text). On null the caller
// clears any open menu and lets the event pass through — the
// native menu is only suppressed once a menu will actually show.
export function buildContextMenuState(e, containerEl) {
  const target = e.target;
  const isInput = target.tagName === 'TEXTAREA' || target.tagName === 'INPUT';
  
  const selection = window.getSelection();
  const selectedText = selection ? selection.toString().trim() : '';

  const thoughtEl = target.closest('.thought-step-content') || target.closest('.chat-thought-container');
  const bubbleEl = target.closest('.chat-bubble');
  
  let fullText = '';
  let isThought = false;
  if (thoughtEl) {
    fullText = thoughtEl.innerText || '';
    isThought = true;
  } else if (bubbleEl) {
    fullText = bubbleEl.innerText || '';
  }

  if (!selectedText && !isInput && !fullText) {
    return null;
  }


  const menuWidth = 190;
  const menuHeight = isInput ? 180 : 130;
  let x = e.clientX;
  let y = e.clientY;
  
  let maxX = window.innerWidth;
  let maxY = window.innerHeight;

  if (containerEl) {
    const rect = containerEl.getBoundingClientRect();
    x -= rect.left;
    y -= rect.top;
    maxX = rect.width;
    maxY = rect.height;
  }

  if (x + menuWidth > maxX - 10) {
    x = maxX - menuWidth - 10;
  }
  if (y + menuHeight > maxY - 10) {
    y = maxY - menuHeight - 10;
  }

  return {
    x,
    y,
    isInput,
    selectedText,
    fullText,
    isThought,
    inputElement: isInput ? target : null,
    targetElement: thoughtEl || bubbleEl || target
  };
}
