import React, { useMemo } from 'react';

const KEYWORDS = new Set([
  'def', 'class', 'return', 'if', 'else', 'elif', 'for', 'while', 'import',
  'from', 'as', 'try', 'except', 'finally', 'raise', 'with', 'async', 'await',
  'function', 'const', 'let', 'var', 'export', 'default', 'true', 'false',
  'null', 'None', 'True', 'False', 'yield', 'lambda', 'self', 'new', 'this',
  'switch', 'case', 'break', 'continue', 'in', 'is', 'not', 'and', 'or'
]);

function highlightTokens(line) {
  // Simple token regex for strings, comments, words, numbers
  const tokenRegex = /(\/\/.*$|#.*$|'(?:\\.|[^'])*'|"(?:\\.|[^"])*"|`(?:\\.|[^`])*`|\b\w+\b|[^\s\w]+|\s+)/g;
  const matches = line.match(tokenRegex) || [line];

  return matches.map((token, i) => {
    if (token.startsWith('//') || token.startsWith('#')) {
      return <span key={i} style={{ color: '#6ee7b7', fontStyle: 'italic' }}>{token}</span>;
    }
    if ((token.startsWith("'") && token.endsWith("'")) ||
        (token.startsWith('"') && token.endsWith('"')) ||
        (token.startsWith('`') && token.endsWith('`'))) {
      return <span key={i} style={{ color: '#fde047' }}>{token}</span>;
    }
    if (KEYWORDS.has(token)) {
      return <span key={i} style={{ color: '#c084fc', fontWeight: 600 }}>{token}</span>;
    }
    if (/^\d+(\.\d+)?$/.test(token)) {
      return <span key={i} style={{ color: '#93c5fd' }}>{token}</span>;
    }
    return <span key={i} style={{ color: '#e2e8f0' }}>{token}</span>;
  });
}

export default function CodeRenderer({ content = '', _fileName = '', _language = '' }) {
  const lines = useMemo(() => {
    return (content || '').split('\n');
  }, [content]);

  return (
    <div
      data-testid="code-renderer"
      style={{
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
        fontSize: '13px',
        lineHeight: '1.6',
        background: 'rgba(15, 15, 20, 0.75)',
        borderRadius: '8px',
        border: '1px solid rgba(255, 255, 255, 0.08)',
        overflowX: 'auto',
        display: 'flex',
        flexDirection: 'column',
        height: '100%'
      }}
    >
      <div style={{ display: 'flex', minWidth: '100%', flex: 1, padding: '12px 0' }}>
        {/* Line numbers gutter */}
        <div
          style={{
            userSelect: 'none',
            textAlign: 'right',
            paddingRight: '14px',
            paddingLeft: '12px',
            color: 'rgba(255, 255, 255, 0.25)',
            borderRight: '1px solid rgba(255, 255, 255, 0.06)'
          }}
        >
          {lines.map((_, idx) => (
            <div key={idx} style={{ height: '21px' }}>
              {idx + 1}
            </div>
          ))}
        </div>

        {/* Code lines */}
        <div style={{ paddingLeft: '14px', paddingRight: '16px', flex: 1, whiteSpace: 'pre' }}>
          {lines.map((line, idx) => (
            <div key={idx} style={{ height: '21px' }}>
              {highlightTokens(line)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
