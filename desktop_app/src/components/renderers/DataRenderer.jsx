import React, { useState, useMemo } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';

function JsonNode({ label, value, defaultOpen = true, depth = 0 }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const isObject = value !== null && typeof value === 'object';
  const isArray = Array.isArray(value);

  if (!isObject) {
    let valStr = String(value);
    let valColor = '#93c5fd';
    if (typeof value === 'string') {
      valStr = `"${value}"`;
      valColor = '#fde047';
    } else if (typeof value === 'boolean') {
      valColor = '#c084fc';
    } else if (value === null) {
      valColor = '#94a3b8';
    }

    return (
      <div style={{ paddingLeft: `${depth * 18}px`, lineHeight: '22px', display: 'flex', gap: '6px' }}>
        {label !== undefined && <span style={{ color: '#e2e8f0', fontWeight: 500 }}>{label}:</span>}
        <span style={{ color: valColor }}>{valStr}</span>
      </div>
    );
  }

  const keys = Object.keys(value);
  const count = keys.length;
  const prefix = isArray ? '[' : '{';
  const suffix = isArray ? ']' : '}';

  return (
    <div style={{ paddingLeft: `${depth * 18}px`, lineHeight: '22px' }}>
      <div
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '4px',
          cursor: 'pointer',
          userSelect: 'none'
        }}
      >
        {isOpen ? <ChevronDown size={14} color="#94a3b8" /> : <ChevronRight size={14} color="#94a3b8" />}
        {label !== undefined && <span style={{ color: '#e2e8f0', fontWeight: 500 }}>{label}:</span>}
        <span style={{ color: 'rgba(255, 255, 255, 0.4)', fontSize: '12px' }}>
          {prefix} {count} {count === 1 ? 'item' : 'items'} {suffix}
        </span>
      </div>
      {isOpen && (
        <div>
          {keys.map((k) => (
            <JsonNode
              key={k}
              label={isArray ? undefined : k}
              value={value[k]}
              depth={depth + 1}
              defaultOpen={depth < 2}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function DataRenderer({ content = '', format = 'json' }) {
  const isJson = format === 'json' || (!format && content.trim().startsWith('{') || content.trim().startsWith('['));

  const parsedJson = useMemo(() => {
    if (!isJson) return null;
    try {
      return JSON.parse(content);
    } catch {
      return null;
    }
  }, [content, isJson]);

  const parsedCsv = useMemo(() => {
    if (isJson || !content) return { headers: [], rows: [] };
    const rawLines = content.trim().split('\n').filter(Boolean);
    if (rawLines.length === 0) return { headers: [], rows: [] };

    const splitCsvRow = (row) => {
      // Basic CSV parser handling simple commas and quoted cells
      const res = [];
      let inQuotes = false;
      let current = '';
      for (let i = 0; i < row.length; i++) {
        const char = row[i];
        if (char === '"' && (i === 0 || row[i - 1] !== '\\')) {
          inQuotes = !inQuotes;
        } else if (char === ',' && !inQuotes) {
          res.push(current.trim());
          current = '';
        } else {
          current += char;
        }
      }
      res.push(current.trim());
      return res;
    };

    const headers = splitCsvRow(rawLines[0]);
    const rows = rawLines.slice(1).map(splitCsvRow);
    return { headers, rows };
  }, [content, isJson]);

  return (
    <div
      data-testid="data-renderer"
      style={{
        fontFamily: isJson ? 'ui-monospace, monospace' : '-apple-system, sans-serif',
        fontSize: '13px',
        background: 'rgba(15, 15, 20, 0.75)',
        borderRadius: '8px',
        border: '1px solid rgba(255, 255, 255, 0.08)',
        height: '100%',
        overflow: 'auto',
        padding: '16px'
      }}
    >
      {isJson ? (
        parsedJson !== null ? (
          <div data-testid="json-tree">
            <JsonNode value={parsedJson} depth={0} defaultOpen={true} />
          </div>
        ) : (
          <div style={{ color: '#fca5a5' }}>
            Invalid JSON: displaying raw content.
            <pre style={{ whiteSpace: 'pre-wrap', color: '#e2e8f0', marginTop: '10px' }}>{content}</pre>
          </div>
        )
      ) : (
        <div data-testid="csv-table" style={{ width: '100%', overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.15)', background: 'rgba(255, 255, 255, 0.03)' }}>
                {parsedCsv.headers.map((h, i) => (
                  <th key={i} style={{ padding: '10px 14px', color: '#a78bfa', fontWeight: 600 }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {parsedCsv.rows.map((r, rowIdx) => (
                <tr
                  key={rowIdx}
                  style={{
                    borderBottom: '1px solid rgba(255, 255, 255, 0.05)',
                    background: rowIdx % 2 === 0 ? 'transparent' : 'rgba(255, 255, 255, 0.02)'
                  }}
                >
                  {r.map((cell, colIdx) => (
                    <td key={colIdx} style={{ padding: '8px 14px', color: '#cbd5e1' }}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
