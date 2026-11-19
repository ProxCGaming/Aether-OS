import React from 'react';
import { Send, Square, X, Scissors, Mic } from 'lucide-react';

// Wave 5 (Annex C row 6, composer seam): the message composer —
// slash-command popup, the plan-mode / snip row, attachment
// previews, the textarea with its push-to-talk mic button, and
// the send / stop buttons — extracted from DraggableChatWindow.
// Presentational by design: the composer state (input text,
// attachments, slash popup, dictation) stays in
// DraggableChatWindow, which owns the send path and the shared
// state plumbing and passes values + handlers down as props.
export default function ChatComposer({
  input,
  attachments,
  isThinking,
  planMode,
  isDictating,
  dictationError,
  showSlashPopup,
  filteredCommands,
  slashIndex,
  slashPopupRef,
  textareaRef,
  onSubmit,
  onInputChange,
  onInputKeyDown,
  onTogglePlanMode,
  onTriggerSnip,
  onRemoveAttachment,
  onSelectSlashCommand,
  onMicStart,
  onMicEnd,
  onMicKeyDown,
  onMicKeyUp,
  onMicClick,
  // Wave 4: the dictation button behaviour ('hold' |
  // 'toggle') — only the label and the click wiring differ;
  // the press handlers above are inert in toggle mode (the
  // host decides), so this component stays a dumb button.
  dictationMode = 'hold',
  onStopTask,
  onDismissDictationError,
}) {
  return (
    <div style={{ padding: '16px', background: 'var(--bg-raised)', borderTop: '1px solid var(--border-default)', position: 'relative' }}>
      {showSlashPopup && filteredCommands.length > 0 && (
        <div
          ref={slashPopupRef}
          className="chat-scroll aether-glass"
          style={{
            position: 'absolute',
            bottom: '100%',
            left: '16px',
            right: '16px',
            marginBottom: '8px',
            padding: '8px',
            display: 'flex',
            flexDirection: 'column',
            gap: '4px',
            maxHeight: '200px',
            overflowY: 'auto',
            zIndex: 100,
          }}
        >
          {filteredCommands.map((cmd, idx) => (
            <div
              key={cmd.command}
              onClick={() => onSelectSlashCommand(cmd)}
              style={{
                padding: '8px 12px',
                borderRadius: '8px',
                background: idx === slashIndex ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent',
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '4px'
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: idx === slashIndex ? 'var(--accent)' : 'var(--text-primary)', fontWeight: 'bold' }}>{cmd.command}</span>
                {cmd.type === 'skill_invoke' && (
                  <span style={{ fontSize: '10px', background: 'rgba(76, 195, 138, 0.18)', color: 'var(--success)', padding: '2px 6px', borderRadius: '4px' }}>SKILL</span>
                )}
              </div>
              <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>{cmd.description}</span>
            </div>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: '8px' }}>
        <button
          type="button"
          className={`plan-mode-toggle ${planMode ? 'active' : ''}`}
          aria-pressed={planMode}
          aria-label="Toggle plan mode"
          title="Plan mode: agents only use read-only tools"
          onClick={() => onTogglePlanMode && onTogglePlanMode()}
        >
          Plan
        </button>
        <button
          type="button"
          className="snip-btn"
          data-testid="snip-ask-btn"
          title="Snip & ask (Ctrl+Shift+S)"
          aria-label="Snip screen region"
          onClick={onTriggerSnip}
          style={{
            marginLeft: '8px',
            background: 'var(--bg-hover)',
            border: '1px solid var(--border-default)',
            borderRadius: '8px',
            padding: '4px 10px',
            color: 'var(--text-secondary)',
            fontSize: '12px',
            fontWeight: 500,
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            transition: 'all 0.15s',
          }}
        >
          <Scissors size={13} color="#F5B942" />
          Snip &amp; ask
        </button>
      </div>
      <form onSubmit={onSubmit} className="ios-glass-input" style={{ 
        display: 'flex', 
        flexDirection: 'column',
        padding: '8px 12px' 
      }}>
        {attachments.length > 0 && (
          <div data-testid="composer-attachments-preview" style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '8px', width: '100%' }}>
            {attachments.map((att, idx) => (
              <div key={idx} style={{ position: 'relative', display: 'inline-block' }}>
                <img
                  data-testid="composer-attachment-thumbnail"
                  src={att.data || att.url || (att.path ? `file://${att.path}` : '')}
                  alt={att.name || 'Attached snip'}
                  style={{ width: '56px', height: '56px', objectFit: 'cover', borderRadius: '8px', border: '1px solid var(--border-strong)' }}
                />
                <button
                  type="button"
                  data-testid="remove-attachment-btn"
                  onClick={() => onRemoveAttachment(idx)}
                  style={{
                    position: 'absolute',
                    top: '-6px',
                    right: '-6px',
                    width: '18px',
                    height: '18px',
                    borderRadius: '50%',
                    background: 'var(--danger)',
                    color: 'var(--text-primary)',
                    border: 'none',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                  title="Remove attachment"
                >
                  <X size={10} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div style={{ display: 'flex', width: '100%', alignItems: 'flex-end' }}>
        <textarea 
          ref={textareaRef}
          value={input}
          onChange={onInputChange}
          onKeyDown={onInputKeyDown}
          placeholder="Message Aether..."
          style={{
            flex: 1,
            background: 'transparent',
            border: 'none',
            color: 'var(--text-primary)',
            outline: 'none',
            resize: 'none',
            minHeight: '24px',
            maxHeight: '120px',
            fontSize: '14px',
            padding: '4px 0',
            fontFamily: 'inherit'
          }}
          rows={1}
        />
        <button
          type="button"
          data-testid="composer-mic-btn"
          className={`composer-mic-btn ${isDictating ? 'recording' : ''}`}
          title={dictationError || (dictationMode === 'toggle'
            ? (isDictating ? 'Click to finish dictation' : 'Click to dictate')
            : (isDictating ? 'Release to finish dictation' : 'Hold to dictate'))}
          aria-label={dictationError || (dictationMode === 'toggle'
            ? (isDictating ? 'Click to finish dictation' : 'Click to dictate')
            : (isDictating ? 'Release to finish dictation' : 'Hold to dictate'))}
          aria-pressed={isDictating ? 'true' : 'false'}
          onClick={onMicClick}
          onMouseDown={onMicStart}
          onMouseUp={onMicEnd}
          onMouseLeave={onMicEnd}
          onTouchStart={onMicStart}
          onTouchEnd={onMicEnd}
          onTouchCancel={onMicEnd}
          onKeyDown={onMicKeyDown}
          onKeyUp={onMicKeyUp}
          style={{
            background: isDictating ? 'rgba(201, 42, 42, 0.25)' : 'var(--bg-hover)',
            border: isDictating ? '1px solid rgba(201, 42, 42, 0.5)' : '1px solid var(--border-default)',
            borderRadius: '50%',
            width: '32px',
            height: '32px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: isDictating ? 'var(--danger)' : 'var(--text-secondary)',
            cursor: 'pointer',
            transition: 'all 0.2s',
            marginLeft: '8px',
            flexShrink: 0,
            boxShadow: isDictating ? '0 0 10px rgba(201, 42, 42, 0.35)' : 'none',
          }}
        >
          <Mic size={14} color={isDictating ? '#C92A2A' : '#A7A7A3'} />
        </button>
        {isThinking ? (
          <>
          {input.trim() && (
            <button
              type="submit"
              title="Queue steering message"
              aria-label="Queue steering message"
              style={{
                background: 'var(--accent)',
                border: 'none',
                borderRadius: '50%',
                width: '32px',
                height: '32px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--accent-on)',
                cursor: 'pointer',
                transition: 'all 0.2s',
                marginLeft: '8px',
                flexShrink: 0
              }}
            >
              <Send size={14} />
            </button>
          )}
          <button 
            type="button"
            onClick={onStopTask}
            title="Stop generation"
            aria-label="Stop generation"
            className="ios-chat-stop-btn"
            style={{
              background: 'rgba(201, 42, 42, 0.2)',
              border: '1px solid rgba(201, 42, 42, 0.4)',
              borderRadius: '50%',
              width: '32px',
              height: '32px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--danger)',
              cursor: 'pointer',
              transition: 'all 0.2s',
              marginLeft: '8px',
              flexShrink: 0,
              boxShadow: '0 0 10px rgba(201, 42, 42, 0.25)'
            }}
          >
            <Square size={12} fill="currentColor" />
          </button>
          </>
        ) : (
          <button 
            type="submit"
            disabled={!input.trim() && attachments.length === 0}
            style={{
              background: (input.trim() || attachments.length > 0) ? 'var(--accent)' : 'var(--bg-hover)',
              border: 'none',
              borderRadius: '50%',
              width: '32px',
              height: '32px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: (input.trim() || attachments.length > 0) ? 'var(--accent-on)' : 'var(--text-muted)',
              cursor: (input.trim() || attachments.length > 0) ? 'pointer' : 'default',
              transition: 'all 0.2s',
              marginLeft: '8px',
              flexShrink: 0
            }}
          >
            <Send size={14} />
          </button>
        )}
        </div>
      </form>
      {dictationError && (
        <div
          data-testid="dictation-error"
          style={{
            color: 'var(--danger)',
            fontSize: '11px',
            marginTop: '6px',
            padding: '4px 8px',
            background: 'rgba(201, 42, 42, 0.1)',
            borderRadius: '4px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <span>{dictationError}</span>
          <button
            type="button"
            onClick={onDismissDictationError}
            style={{ background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', padding: '0 4px' }}
          >
            ×
          </button>
        </div>
      )}
      <div style={{ fontSize: '10px', color: 'var(--text-muted)', textAlign: 'center', marginTop: '8px' }}>
        Press Enter to send, Shift+Enter for new line
      </div>
    </div>
  );
}
