import React, { useMemo } from 'react';

const KEYWORDS = new Set([
  'def', 'class', 'return', 'if', 'else', 'elif', 'for', 'while', 'import',
  'from', 'as', 'try', 'except', 'finally', 'raise', 'with', 'async', 'await',
  'function', 'const', 'let', 'var', 'export', 'default', 'true', 'false',
  'null', 'None', 'True', 'False', 'yield', 'lambda', 'self', 'new', 'this',
  'switch', 'case', 'break', 'continue', 'in', 'is', 'not', 'and', 'or'
]);

// D3-15: the language contract is real now — the tokenizer's
// comment rule depends on the language, resolved from the explicit
// `language` prop first and the file extension second. The sets key
// on both language names and extensions. A `#` starts a comment
// only in hash-comment languages; `//` only in slash-comment ones;
// languages with neither (CSS, HTML, JSON…) get no line comments at
// all. Unknown languages keep the old both-styles behaviour rather
// than guessing.
const HASH_COMMENT_KEYS = new Set([
  'python', 'py', 'shell', 'bash', 'sh', 'zsh', 'yaml', 'yml', 'toml',
  'ruby', 'rb', 'perl', 'pl', 'r', 'powershell', 'ps1', 'dockerfile',
  'makefile', 'make', 'ini', 'cfg', 'conf',
]);
const SLASH_COMMENT_KEYS = new Set([
  'javascript', 'js', 'jsx', 'typescript', 'ts', 'tsx', 'java', 'c',
  'cpp', 'cxx', 'cc', 'h', 'hpp', 'csharp', 'cs', 'go', 'rust', 'rs',
  'php', 'swift', 'kotlin', 'kt', 'scala', 'objc', 'm',
]);
const NO_LINE_COMMENT_KEYS = new Set([
  'css', 'scss', 'less', 'html', 'htm', 'xml', 'svg', 'json',
]);

function commentKindFor(language, fileName) {
  let key = (language || '').trim().toLowerCase();
  if (!key && fileName) {
    const dot = fileName.lastIndexOf('.');
    if (dot >= 0) key = fileName.slice(dot + 1).toLowerCase();
  }
  if (HASH_COMMENT_KEYS.has(key)) return 'hash';
  if (SLASH_COMMENT_KEYS.has(key)) return 'slash';
  if (NO_LINE_COMMENT_KEYS.has(key)) return 'none';
  return 'both';
}

const STRING_ALTS = `'(?:\\\\.|[^'])*'|"(?:\\\\.|[^"])*"|\`(?:\\\\.|[^\`])*\``;
const TOKEN_REGEXES = {
  both: new RegExp(`(\\/\\/.*$|#.*$|${STRING_ALTS}|\\b\\w+\\b|[^\\s\\w]+|\\s+)`, 'g'),
  slash: new RegExp(`(\\/\\/.*$|${STRING_ALTS}|\\b\\w+\\b|[^\\s\\w]+|\\s+)`, 'g'),
  hash: new RegExp(`(#.*$|${STRING_ALTS}|\\b\\w+\\b|[^\\s\\w]+|\\s+)`, 'g'),
  none: new RegExp(`(${STRING_ALTS}|\\b\\w+\\b|[^\\s\\w]+|\\s+)`, 'g'),
};

function isCommentToken(token, kind) {
  if (kind === 'hash' || kind === 'both') {
    if (token.startsWith('#')) return true;
  }
  if (kind === 'slash' || kind === 'both') {
    if (token.startsWith('//')) return true;
  }
  return false;
}

function highlightTokens(line, commentKind) {
  const tokenRegex = TOKEN_REGEXES[commentKind] || TOKEN_REGEXES.both;
  const matches = line.match(tokenRegex) || [line];

  return matches.map((token, i) => {
    if (isCommentToken(token, commentKind)) {
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

// D3-15 (size guard): rendering is one DOM row of spans per line —
// beyond this many lines the view becomes a memory hazard, so the
// tail is elided with an honest count instead of rendered.
export const MAX_RENDERED_LINES = 2000;

export default function CodeRenderer({ content = '', fileName = '', language = '' }) {
  const commentKind = commentKindFor(language, fileName);
  const { lines, hiddenCount } = useMemo(() => {
    const all = (content || '').split('\n');
    if (all.length > MAX_RENDERED_LINES) {
      return { lines: all.slice(0, MAX_RENDERED_LINES), hiddenCount: all.length - MAX_RENDERED_LINES };
    }
    return { lines: all, hiddenCount: 0 };
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
              {highlightTokens(line, commentKind)}
            </div>
          ))}
          {hiddenCount > 0 && (
            <div
              data-testid="code-truncated-note"
              style={{ height: '21px', color: 'rgba(255, 255, 255, 0.4)', fontStyle: 'italic' }}
            >
              {`… ${hiddenCount} more lines not shown`}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
