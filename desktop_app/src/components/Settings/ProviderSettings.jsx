import React, { useState, useEffect, useRef } from 'react';
import { Key, CheckCircle2 } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

export default function ProviderSettings({ providers = [], health = {} }) {
  const { wsRef, sendMessage } = useAetherSocket();
  const [keys, setKeys] = useState({});
  const [baseUrls, setBaseUrls] = useState({});
  const [expandedProvider, setExpandedProvider] = useState(null);
  const [validationState, setValidationState] = useState({});
  const [validationMsg, setValidationMsg] = useState({});
  const [expandedModels, setExpandedModels] = useState({});
  const [customDisplayNames, setCustomDisplayNames] = useState({});
  // Test-button hardening: a ref guard (state can lag a fast
  // double-click), a per-provider client timeout, and an elapsed
  // clock so a running test is unmistakably visible.
  const testingRef = useRef({});
  const testTimersRef = useRef({});
  const [testingSince, setTestingSince] = useState({});
  const [nowTick, setNowTick] = useState(Date.now());

  useEffect(() => {
    if (Object.keys(testingSince).length === 0) return;
    const id = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [testingSince]);

  useEffect(() => () => {
    Object.values(testTimersRef.current).forEach(clearTimeout);
  }, []);

  const clearTestTracking = (provider) => {
    testingRef.current[provider] = false;
    if (testTimersRef.current[provider]) {
      clearTimeout(testTimersRef.current[provider]);
      delete testTimersRef.current[provider];
    }
    setTestingSince(prev => {
      if (!(provider in prev)) return prev;
      const next = { ...prev };
      delete next[provider];
      return next;
    });
  };

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PROVIDER_VALIDATE_RESPONSE' || data.type === 'SETTINGS_PROVIDER_VALIDATE_RESULT') {
          const provider = data.payload.provider;
          clearTestTracking(provider);
          const discErr = data.payload.discovery_error;
          const discErrText = discErr
            ? ` (model list failed: ${discErr.code ? 'HTTP ' + discErr.code : 'no response'} — ${discErr.message})`
            : '';
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success' }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Test Successful' + discErrText }));
            setTimeout(() => setValidationState(prev => ({ ...prev, [provider]: null })), 3000);
          } else {
            setValidationState(prev => ({ ...prev, [provider]: 'error' }));
            setValidationMsg(prev => ({ ...prev, [provider]: data.payload.error || data.payload.message || 'Validation failed' }));
          }
        } else if (data.type === 'PROVIDER_SAVE_RESPONSE') {
          const provider = data.payload.provider;
          const isCustomSave = provider.startsWith('custom_');
          const discErr = data.payload.discovery_error;
          const discErrText = discErr
            ? ` Model list failed (${discErr.code ? 'HTTP ' + discErr.code : 'no response'}): ${discErr.message}`
            : '';
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success', ...(isCustomSave ? { custom_openai: 'success' } : {}) }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Saved!' + discErrText, ...(isCustomSave ? { custom_openai: 'Saved!' + discErrText } : {}) }));
            setTimeout(() => setValidationState(prev => ({ ...prev, [provider]: null, ...(isCustomSave ? { custom_openai: null } : {}) })), 3000);
          } else {
            setValidationState(prev => ({ ...prev, [provider]: 'error', ...(isCustomSave ? { custom_openai: 'error' } : {}) }));
            setValidationMsg(prev => ({ ...prev, [provider]: data.payload.error || 'Failed to save', ...(isCustomSave ? { custom_openai: data.payload.error || 'Failed to save' } : {}) }));
          }
        }
      } catch (e) {
        // ignore
      }
    };
    
    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef]);

  const handleSaveKey = (providerName) => {
    let finalProviderName = providerName;
    let displayName = null;

    if (providerName === 'custom_openai') {
      const name = customDisplayNames[providerName];
      if (!name) {
        setValidationState(prev => ({ ...prev, [providerName]: 'error' }));
        setValidationMsg(prev => ({ ...prev, [providerName]: 'Display name required' }));
        return;
      }
      finalProviderName = `custom_${name.toLowerCase().replace(/[^a-z0-9]/g, '_')}`;
      displayName = name;
    }

    setValidationState(prev => ({ ...prev, [providerName]: 'saving' }));
    
    const apiKeyToSave = keys[providerName] || '';
    // Phase 10.1 C1: keys are NEVER mirrored to localStorage (or any other
    // renderer storage). The engine's secure secret store is the only
    // destination — the key goes out via PROVIDER_SAVE_REQUEST only.
    sendMessage('PROVIDER_SAVE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        provider: finalProviderName,
        api_key: apiKeyToSave,
        base_url: baseUrls[providerName] || '',
        ...(displayName && { display_name: displayName, provider_type: 'openai_compatible' })
      }
    });
  };

  const handleTestKey = (providerName) => {
    // Repeat clicks while a test is running are ignored outright —
    // each request used to queue another full engine-side validation.
    if (testingRef.current[providerName]) return;
    testingRef.current[providerName] = true;
    setValidationState(prev => ({ ...prev, [providerName]: 'testing' }));
    setValidationMsg(prev => ({ ...prev, [providerName]: '' }));
    setTestingSince(prev => ({ ...prev, [providerName]: Date.now() }));
    // The engine caps validation at 30s; if nothing comes back in
    // 35s, say so plainly instead of sitting on "Testing..." forever.
    testTimersRef.current[providerName] = setTimeout(() => {
      if (testingRef.current[providerName]) {
        clearTestTracking(providerName);
        setValidationState(prev => ({ ...prev, [providerName]: 'error' }));
        setValidationMsg(prev => ({ ...prev, [providerName]: 'No answer from the engine in 35s — it may be busy or offline. Try again.' }));
      }
    }, 35000);
    sendMessage('SETTINGS_PROVIDER_VALIDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        provider: providerName,
        api_key: keys[providerName] || '',
        base_url: baseUrls[providerName] || ''
      }
    });
  };

  const handleDisconnect = (e, providerName) => {
    e.stopPropagation();
    sendMessage('PROVIDER_REMOVE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        provider: providerName
      }
    });
    setExpandedProvider(null);
  };

  return (
    <div style={{ color: '#f0f0f5' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>API Providers</h2>
      <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Configure your API keys for cloud-based inference.</p>
      
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {(Array.isArray(providers) ? providers : []).map(p => {
          const providerName = p.name;
          const displayName = p.display_name || (providerName.charAt(0).toUpperCase() + providerName.slice(1));
          const isConfigured = p.has_key;
          const isCustom = providerName.startsWith('custom_') || providerName === 'custom_openai';
          const isExpanded = expandedProvider === providerName;
          const healthInfo = health && typeof health === 'object' ? health[providerName] : null;
          const healthColor = !healthInfo ? null : healthInfo.status === 'healthy' ? '#10b981' : healthInfo.status === 'degraded' ? '#f59e0b' : '#ef4444';
          
          return (
            <div key={providerName} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', overflow: 'hidden', transition: 'all 0.3s' }}>
              <div 
                onClick={() => setExpandedProvider(isExpanded ? null : providerName)}
                style={{ padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', background: isExpanded ? 'rgba(255,255,255,0.02)' : 'transparent' }}
              >
                <h3 style={{ fontSize: '16px', margin: 0 }}>{displayName}</h3>
                <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                  {healthInfo && (
                    <div data-testid={`provider-health-${providerName}`} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: healthColor }}>
                      <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: healthColor, display: 'inline-block' }} />
                      {healthInfo.status}{healthInfo.latency_ms > 0 ? ` · ${Math.round(healthInfo.latency_ms)} ms` : ''}
                    </div>
                  )}
                  {isConfigured ? (
                    <>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#10b981', fontSize: '13px' }}>
                        <CheckCircle2 size={16} /> Configured
                      </div>
                      <button 
                        onClick={(e) => handleDisconnect(e, providerName)}
                        style={{ background: 'rgba(242, 65, 91, 0.15)', color: '#FA5870', border: '1px solid rgba(242, 65, 91, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s', fontSize: '13px' }}
                      >
                        Disconnect
                      </button>
                    </>
                  ) : (
                    <>
                      <div style={{ fontSize: '13px', color: '#9090a0' }}>Not Configured</div>
                      <button 
                        style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: '1px solid rgba(255,255,255,0.2)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s', fontSize: '13px' }}
                      >
                        {isExpanded ? 'Close' : 'Connect'}
                      </button>
                    </>
                  )}
                </div>
              </div>
              
              {isExpanded && (
                <div style={{ padding: '0 20px 20px 20px', display: 'flex', flexDirection: 'column', gap: '8px', borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: '16px' }}>
                  {providerName === 'custom_openai' && (
                    <input 
                      type="text" 
                      value={customDisplayNames[providerName] || ''}
                      onChange={(e) => setCustomDisplayNames({...customDisplayNames, [providerName]: e.target.value})}
                      className="ios-glass-input" 
                      placeholder="Display Name (e.g. LM Studio)" 
                      style={{ padding: '12px 16px', color: '#fff', outline: 'none', width: '100%', marginBottom: '4px' }}
                    />
                  )}
                  {isCustom && (
                    <input 
                      type="text" 
                      value={baseUrls[providerName] || ''}
                      onChange={(e) => setBaseUrls({...baseUrls, [providerName]: e.target.value})}
                      className="ios-glass-input" 
                      placeholder="Base URL (e.g. http://localhost:1234/v1)" 
                      style={{ padding: '12px 16px', color: '#fff', outline: 'none', width: '100%', marginBottom: '4px' }}
                    />
                  )}
                  <input 
                    type="password" 
                    value={keys[providerName] || ''}
                    onChange={(e) => setKeys({...keys, [providerName]: e.target.value})}
                    className="ios-glass-input" 
                    placeholder={isConfigured ? `API Key (Stored Securely)` : `Enter ${displayName} API Key`} 
                    style={{ padding: '12px 16px', color: '#fff', outline: 'none', width: '100%' }}
                  />
                  
                  {validationState[providerName] === 'error' && (
                    <div style={{ color: '#ef4444', fontSize: '13px', marginTop: '4px' }}>{validationMsg[providerName]}</div>
                  )}
                  {validationState[providerName] === 'success' && (
                    <div style={{ color: '#10b981', fontSize: '13px', marginTop: '4px' }}>{validationMsg[providerName]}</div>
                  )}

                  <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '8px', gap: '8px' }}>
                    <button 
                      onClick={() => handleTestKey(providerName)}
                      disabled={validationState[providerName] === 'testing'}
                      style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: '1px solid rgba(255,255,255,0.2)', padding: '8px 24px', borderRadius: '12px', cursor: 'pointer', transition: 'all 0.2s', fontWeight: 500 }}
                    >
                      {validationState[providerName] === 'testing' ? (
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
                          <span className="aether-spinner" style={{ width: '12px', height: '12px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.3)', borderTopColor: '#fff', display: 'inline-block' }} />
                          Testing…{testingSince[providerName] ? ` ${Math.max(0, Math.floor((nowTick - testingSince[providerName]) / 1000))}s` : ''}
                        </span>
                      ) : 'Test'}
                    </button>
                    <button 
                      onClick={() => handleSaveKey(providerName)}
                      disabled={validationState[providerName] === 'saving'}
                      style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '12px', cursor: 'pointer', transition: 'all 0.2s', fontWeight: 500 }}
                    >
                      {validationState[providerName] === 'saving' ? 'Saving...' : 'Save Configuration'}
                    </button>
                  </div>

                  {validationState[providerName] === 'testing' && (
                    <div style={{ color: '#9090a0', fontSize: '12px', marginTop: '6px' }}>Checking the key with the provider — this can take up to ~30s.</div>
                  )}

                  {((p.models && p.models.length > 0) || (p.excluded_models && p.excluded_models.length > 0)) && (
                    <div style={{ marginTop: '12px', borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: '12px' }}>
                      <button
                        onClick={() => setExpandedModels(prev => ({ ...prev, [providerName]: !prev[providerName] }))}
                        style={{ background: 'transparent', color: '#a78bfa', border: 'none', fontSize: '13px', cursor: 'pointer', padding: 0 }}
                      >
                        {expandedModels[providerName] ? '▾ Hide' : `▸ View all ${(p.models?.length || 0) + (p.excluded_models?.length || 0)} models`}
                      </button>
                      {expandedModels[providerName] && (
                        <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '220px', overflowY: 'auto' }}>
                          {(p.models || []).map(m => (
                            <div key={m} style={{ fontSize: '13px', color: '#e2e8f0', fontFamily: 'monospace', padding: '4px 8px', background: 'rgba(255,255,255,0.02)', borderRadius: '6px' }}>
                              {m}
                            </div>
                          ))}
                          {(p.excluded_models || []).map(m => {
                            const id = typeof m === 'string' ? m : m.id;
                            const kind = typeof m === 'string' ? 'other' : (m.kind || 'other');
                            const kindColors = { embedding: '#60a5fa', image: '#c084fc', audio: '#34d399', other: '#9ca3af' };
                            return (
                              <div key={id} style={{ fontSize: '13px', color: '#9ca3af', fontFamily: 'monospace', padding: '4px 8px', background: 'rgba(255,255,255,0.02)', borderRadius: '6px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <span>{id}</span>
                                <span style={{ fontSize: '11px', color: kindColors[kind] || kindColors.other, border: `1px solid ${kindColors[kind] || kindColors.other}`, borderRadius: '8px', padding: '1px 8px', fontFamily: 'sans-serif' }}>{kind}</span>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
        <div style={{ height: '64px', flexShrink: 0 }} />
      </div>
    </div>
  );
}
