import React, { useMemo } from 'react';

export default function DiffRenderer({ diff = '', _filePath = '' }) {
  const parsedLines = useMemo(() => {
    if (!diff) return [];
    const raw = diff.split('\n');
    let oldLine = 0;
    let newLine = 0;

    return raw.map((line, idx) => {
      let type = 'context';
      let oldNum = '';
      let newNum = '';

      if (line.startsWith('@@')) {
        type = 'hunk';
        // Parse hunk header: @@ -oldStart,oldLen +newStart,newLen @@
        const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (match) {
          oldLine = parseInt(match[1], 10);
          newLine = parseInt(match[2], 10);
        }
      } else if (line.startsWith('---') || line.startsWith('+++')) {
        type = 'meta';
      } else if (line.startsWith('+')) {
        type = 'added';
        newNum = newLine++;
      } else if (line.startsWith('-')) {
        type = 'removed';
        oldNum = oldLine++;
      } else if (line.trim().length > 0 || idx < raw.length - 1) {
        type = 'context';
        oldNum = oldLine++;
        newNum = newLine++;
      }

      return { line, type, oldNum, newNum };
    });
  }, [diff]);

  return (
    <div
      data-testid="diff-renderer"
      style={{
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
        fontSize: '13px',
        lineHeight: '1.6',
        background: 'rgba(15, 15, 20, 0.75)',
        borderRadius: '8px',
        border: '1px solid rgba(255, 255, 255, 0.08)',
        overflowX: 'auto',
        height: '100%',
        padding: '12px 0'
      }}
    >
      {parsedLines.map((item, idx) => {
        let bg = 'transparent';
        let color = '#e2e8f0';
        let className = 'diff-line';

        if (item.type === 'hunk') {
          bg = 'rgba(167, 139, 250, 0.12)';
          color = '#c084fc';
          className = 'diff-line diff-hunk-header';
        } else if (item.type === 'added') {
          bg = 'rgba(16, 185, 129, 0.16)';
          color = '#6ee7b7';
          className = 'diff-line diff-line-added';
        } else if (item.type === 'removed') {
          bg = 'rgba(239, 68, 68, 0.16)';
          color = '#fca5a5';
          className = 'diff-line diff-line-removed';
        } else if (item.type === 'meta') {
          color = 'rgba(255, 255, 255, 0.4)';
          className = 'diff-line diff-line-meta';
        }

        return (
          <div
            key={idx}
            className={className}
            style={{
              display: 'flex',
              background: bg,
              color: color,
              minWidth: '100%',
              whiteSpace: 'pre',
              height: '22px',
              alignItems: 'center'
            }}
          >
            {/* Old line num */}
            <span
              style={{
                width: '42px',
                textAlign: 'right',
                paddingRight: '8px',
                color: 'rgba(255, 255, 255, 0.25)',
                userSelect: 'none',
                flexShrink: 0
              }}
            >
              {item.oldNum}
            </span>
            {/* New line num */}
            <span
              style={{
                width: '42px',
                textAlign: 'right',
                paddingRight: '12px',
                color: 'rgba(255, 255, 255, 0.25)',
                userSelect: 'none',
                borderRight: '1px solid rgba(255, 255, 255, 0.08)',
                flexShrink: 0
              }}
            >
              {item.newNum}
            </span>
            {/* Line content */}
            <span className={className} style={{ paddingLeft: '12px', paddingRight: '16px', flex: 1 }}>
              {item.line}
            </span>
          </div>
        );
      })}
    </div>
  );
}
