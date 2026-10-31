import React from 'react';
import ReactMarkdown from 'react-markdown';

export default function MarkdownRenderer({ content = '' }) {
  return (
    <div
      data-testid="markdown-renderer"
      style={{
        padding: '20px 24px',
        color: '#e2e8f0',
        fontSize: '14px',
        lineHeight: '1.7',
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
        background: 'rgba(15, 15, 20, 0.75)',
        borderRadius: '8px',
        border: '1px solid rgba(255, 255, 255, 0.08)',
        height: '100%',
        overflowY: 'auto'
      }}
      className="markdown-content"
    >
      <ReactMarkdown>{content || ''}</ReactMarkdown>
    </div>
  );
}
