import React, { useState, useEffect, useRef } from 'react';
import { ChevronDown, Check } from 'lucide-react';

export default function Dropdown({
  value,
  onChange,
  options = [],
  placeholder = 'Select...',
  style = {},
  align = 'left'
}) {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef(null);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  // Find the selected label
  let selectedLabel = placeholder;
  for (const opt of options) {
    if (opt.group) {
      const found = opt.items.find(i => i.value === value);
      if (found) {
        selectedLabel = found.label;
        break;
      }
    } else {
      if (opt.value === value) {
        selectedLabel = opt.label;
        break;
      }
    }
  }

  return (
    <div style={{ position: 'relative', width: style.width || 'auto' }} ref={dropdownRef}>
      <button 
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '6px',
          padding: '8px 12px',
          background: 'rgba(255,255,255,0.05)',
          border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: '8px',
          color: '#e2e8f0',
          fontSize: '13px',
          cursor: 'pointer',
          transition: 'all 0.2s',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
          width: '100%',
          ...style
        }}
        onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.1)'}
        onMouseLeave={e => e.currentTarget.style.background = 'rgba(255,255,255,0.05)'}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {selectedLabel}
        </span>
        <ChevronDown size={14} style={{ opacity: 0.7, transform: isOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s cubic-bezier(0.16, 1, 0.3, 1)' }} />
      </button>

      {isOpen && (
        <div 
          className="ios-glass"
          style={{
            position: 'absolute',
            top: 'calc(100% + 8px)',
            [align === 'right' ? 'right' : 'left']: 0,
            minWidth: '240px',
            maxHeight: '300px',
            overflowY: 'auto',
            background: 'rgba(15, 15, 25, 0.95)',
            backdropFilter: 'blur(20px)',
            border: '1px solid rgba(255,255,255,0.08)',
            borderRadius: '12px',
            boxShadow: '0 16px 40px rgba(0,0,0,0.4)',
            padding: '8px',
            zIndex: 1000,
            display: 'flex',
            flexDirection: 'column',
            animation: 'fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
            transformOrigin: 'top ' + align
          }}
        >
          {options.map((opt, idx) => {
            if (opt.group) {
              return (
                <div key={opt.group || idx} style={{ marginBottom: '8px' }}>
                  <div style={{ fontSize: '10px', textTransform: 'uppercase', color: '#9090a0', padding: '6px 8px 4px 8px', letterSpacing: '0.05em', fontWeight: 600 }}>
                    {opt.group}
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                    {opt.items.map(item => {
                      const isSelected = item.value === value;
                      return (
                        <button
                          key={item.value}
                          type="button"
                          onClick={() => {
                            onChange(item.value);
                            setIsOpen(false);
                          }}
                          style={{
                            width: '100%',
                            textAlign: 'left',
                            padding: '8px 10px',
                            background: isSelected ? 'rgba(124, 58, 237, 0.2)' : 'transparent',
                            border: 'none',
                            borderRadius: '6px',
                            color: isSelected ? '#a78bfa' : '#e2e8f0',
                            fontSize: '13px',
                            cursor: 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            transition: 'background 0.2s'
                          }}
                          onMouseEnter={e => {
                            if (!isSelected) e.currentTarget.style.background = 'rgba(255,255,255,0.05)';
                          }}
                          onMouseLeave={e => {
                            if (!isSelected) e.currentTarget.style.background = 'transparent';
                          }}
                        >
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</span>
                          {isSelected && <Check size={14} color="#a78bfa" />}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            } else {
              const isSelected = opt.value === value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => {
                    onChange(opt.value);
                    setIsOpen(false);
                  }}
                  style={{
                    width: '100%',
                    textAlign: 'left',
                    padding: '8px 10px',
                    background: isSelected ? 'rgba(124, 58, 237, 0.2)' : 'transparent',
                    border: 'none',
                    borderRadius: '6px',
                    color: isSelected ? '#a78bfa' : '#e2e8f0',
                    fontSize: '13px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    transition: 'background 0.2s'
                  }}
                  onMouseEnter={e => {
                    if (!isSelected) e.currentTarget.style.background = 'rgba(255,255,255,0.05)';
                  }}
                  onMouseLeave={e => {
                    if (!isSelected) e.currentTarget.style.background = 'transparent';
                  }}
                >
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{opt.label}</span>
                  {isSelected && <Check size={14} color="#a78bfa" />}
                </button>
              );
            }
          })}
        </div>
      )}
    </div>
  );
}
