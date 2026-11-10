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
  const scrollRef = useRef(null);

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

  // Custom butter-smooth scrolling using lerp (linear interpolation)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    let targetScroll = el.scrollTop;
    let isScrolling = false;
    let frameId;

    const updateScroll = () => {
      if (!el) return;
      
      // Interpolate towards the target
      el.scrollTop += (targetScroll - el.scrollTop) * 0.15;
      
      if (Math.abs(targetScroll - el.scrollTop) > 0.5) {
        frameId = requestAnimationFrame(updateScroll);
      } else {
        el.scrollTop = targetScroll;
        isScrolling = false;
      }
    };

    const onWheel = (e) => {
      e.preventDefault();
      // Accumulate scroll target
      targetScroll = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, targetScroll + e.deltaY));
      
      if (!isScrolling) {
        isScrolling = true;
        frameId = requestAnimationFrame(updateScroll);
      }
    };

    const onScroll = () => {
      if (!isScrolling) {
        targetScroll = el.scrollTop;
      }
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('scroll', onScroll, { passive: true });
    
    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('scroll', onScroll);
      if (frameId) cancelAnimationFrame(frameId);
    };
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
    <div className="nodrag" style={{ position: 'relative', width: style.width || 'auto', WebkitAppRegion: 'no-drag', cursor: 'auto' }} ref={dropdownRef}>
      <button 
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '6px',
          padding: '8px 12px',
          background: 'var(--bg-base)',
          border: '1px solid var(--border-default)',
          borderRadius: 'var(--radius-md)',
          color: 'var(--text-primary)',
          fontSize: '13px',
          cursor: 'pointer',
          transition: 'all 0.2s',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
          width: '100%',
          ...style
        }}
        onMouseEnter={e => e.currentTarget.style.background = 'var(--bg-hover)'}
        onMouseLeave={e => e.currentTarget.style.background = 'var(--bg-base)'}
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
            borderRadius: '16px',
            padding: '8px',
            boxShadow: '0 20px 40px rgba(0,0,0,0.5), inset 0 1px 1px rgba(255,255,255,0.15), inset 0 0 20px rgba(var(--accent-rgb),0.15), inset 0 -1px 1px rgba(0,0,0,0.3)',
            zIndex: 1000,
            animation: 'fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
            transformOrigin: 'top ' + align,
            overflow: 'hidden'
          }}
        >
          <div 
            ref={scrollRef}
            className="chat-scroll"
            style={{
              maxHeight: '300px',
              overflowY: 'auto',
              paddingRight: '4px',
              display: 'flex',
              flexDirection: 'column'
            }}
          >
          {options.map((opt, idx) => {
            if (opt.group) {
              return (
                <div key={opt.group || idx} style={{ marginBottom: '8px' }}>
                  <div style={{ 
                    position: 'sticky',
                    top: 0,
                    zIndex: 10,
                    background: 'var(--bg-overlay)',
                    backdropFilter: 'blur(8px)',
                    fontSize: '10px', 
                    textTransform: 'uppercase', 
                    color: 'var(--text-secondary)', 
                    padding: '6px 8px 4px 8px', 
                    letterSpacing: '0.05em', 
                    fontWeight: 600,
                    borderBottom: '1px solid var(--border-default)',
                    borderRadius: '4px 4px 0 0'
                  }}>
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
                            // D2-19: grouped items honour `disabled`
                            // exactly like the flat path below — the
                            // two paths must not disagree on the
                            // component's contract.
                            if (item.disabled) return;
                            onChange(item.value);
                            setIsOpen(false);
                          }}
                          style={{
                            width: '100%',
                            textAlign: 'left',
                            padding: '8px 10px',
                            background: isSelected ? 'rgba(var(--accent-rgb), 0.2)' : 'transparent',
                            border: 'none',
                            borderRadius: '6px',
                            color: isSelected ? 'var(--accent)' : (item.disabled ? 'var(--text-muted)' : 'var(--text-primary)'),
                            fontSize: '13px',
                            cursor: item.disabled ? 'not-allowed' : 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            transition: 'background 0.2s',
                            opacity: item.disabled ? 0.6 : 1
                          }}
                          onMouseEnter={e => {
                            if (!isSelected && !item.disabled) e.currentTarget.style.background = 'var(--bg-hover)';
                          }}
                          onMouseLeave={e => {
                            if (!isSelected) e.currentTarget.style.background = 'transparent';
                          }}
                          disabled={item.disabled}
                        >
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</span>
                          {isSelected && <Check size={14} color="var(--accent)" />}
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
                    if (opt.disabled) return;
                    onChange(opt.value);
                    setIsOpen(false);
                  }}
                  style={{
                    width: '100%',
                    textAlign: 'left',
                    padding: '8px 10px',
                    background: isSelected ? 'rgba(var(--accent-rgb), 0.2)' : 'transparent',
                    border: 'none',
                    borderRadius: '6px',
                    color: isSelected ? 'var(--accent)' : (opt.disabled ? 'var(--text-muted)' : 'var(--text-primary)'),
                    fontSize: '13px',
                    cursor: opt.disabled ? 'not-allowed' : 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    transition: 'background 0.2s',
                    opacity: opt.disabled ? 0.6 : 1
                  }}
                  onMouseEnter={e => {
                    if (!isSelected && !opt.disabled) e.currentTarget.style.background = 'var(--bg-hover)';
                  }}
                  onMouseLeave={e => {
                    if (!isSelected) e.currentTarget.style.background = 'transparent';
                  }}
                  disabled={opt.disabled}
                >
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{opt.label}</span>
                  {isSelected && <Check size={14} color="var(--accent)" />}
                </button>
              );
            }
          })}
          </div>
        </div>
      )}
    </div>
  );
}
