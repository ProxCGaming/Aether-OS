import React, { useState, useEffect } from 'react';
import { Key, CheckCircle2 } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

export default function ProviderSettings({ providers = [] }) {
  const { wsRef, sendMessage } = useAetherSocket();
  const [keys, setKeys] = useState({});
  const [baseUrls, setBaseUrls] = useState({});
  const [expandedProvider, setExpandedProvider] = useState(null);
  const [validationState, setValidationState] = useState({});
  const [validationMsg, setValidationMsg] = useState({});
  const [customDisplayNames, setCustomDisplayNames] = useState({});

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PROVIDER_VALIDATE_RESPONSE' || data.type === 'SETTINGS_PROVIDER_VALIDATE_RESULT') {
          const provider = data.payload.provider;
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success' }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Test Successful' }));
            setTimeout(() => setValidationState(prev => ({ ...prev, [provider]: null })), 3000);
          } else {
            setValidationState(prev => ({ ...prev, [provider]: 'error' }));
            setValidationMsg(prev => ({ ...prev, [provider]: data.payload.error || data.payload.message || 'Validation failed' }));
          }
        } else if (data.type === 'PROVIDER_SAVE_RESPONSE') {
          const provider = data.payload.provider;
          const isCustomSave = provider.startsWith('custom_');
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success', ...(isCustomSave ? { custom_openai: 'success' } : {}) }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Saved!', ...(isCustomSave ? { custom_openai: 'Saved!' } : {}) }));
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
    if (apiKeyToSave) {
      localStorage.setItem(`aether_saved_key_${finalProviderName}`, apiKeyToSave);
    }

    sendMessage('PROVIDER_SAVE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      provider: finalProviderName,
      api_key: apiKeyToSave,
      base_url: baseUrls[providerName] || '',
      ...(displayName && { display_name: displayName, provider_type: 'openai_compatible' })
    });
  };

  const handleTestKey = (providerName) => {
    setValidationState(prev => ({ ...prev, [providerName]: 'testing' }));
    sendMessage('SETTINGS_PROVIDER_VALIDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      provider: providerName,
      api_key: keys[providerName] || '',
      base_url: baseUrls[providerName] || ''
    });
  };

  const handleDisconnect = (e, providerName) => {
    e.stopPropagation();
    sendMessage('PROVIDER_REMOVE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      provider: providerName
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
          
          return (
            <div key={providerName} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', overflow: 'hidden', transition: 'all 0.3s' }}>
              <div 
                onClick={() => setExpandedProvider(isExpanded ? null : providerName)}
                style={{ padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', background: isExpanded ? 'rgba(255,255,255,0.02)' : 'transparent' }}
              >
                <h3 style={{ fontSize: '16px', margin: 0 }}>{displayName}</h3>
                <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
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
                      {validationState[providerName] === 'testing' ? 'Testing...' : 'Test'}
                    </button>
                    <button 
                      onClick={() => handleSaveKey(providerName)}
                      disabled={validationState[providerName] === 'saving'}
                      style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '12px', cursor: 'pointer', transition: 'all 0.2s', fontWeight: 500 }}
                    >
                      {validationState[providerName] === 'saving' ? 'Saving...' : 'Save Configuration'}
                    </button>
                  </div>
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
