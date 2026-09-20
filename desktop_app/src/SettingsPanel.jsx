import React, { useState, useEffect } from 'react';
import { User, Key, Cpu, Settings as SettingsIcon, Monitor, Activity, Download, Network, Box, Layers, Plug, Database, Sliders, CheckCircle2 } from 'lucide-react';
import Dropdown from './Dropdown';
import './chat.css';

export default function SettingsPanel({ wsRef, providers = {}, localModels = [], defaultModel = '' }) {
  const [activeMenu, setActiveMenu] = useState('Providers');
  const [modelTab, setModelTab] = useState('cloud'); // 'cloud' or 'local'
  const [keys, setKeys] = useState({});
  const [baseUrls, setBaseUrls] = useState({});
  const [expandedProvider, setExpandedProvider] = useState(null);
  const [validationState, setValidationState] = useState({}); // { provider: 'testing' | 'saving' | 'success' | 'error' }
  const [validationMsg, setValidationMsg] = useState({});
  const [customDisplayNames, setCustomDisplayNames] = useState({}); // for custom_openai
  
  const [downloadInput, setDownloadInput] = useState('');

  useEffect(() => {
    if (!wsRef.current) return;
    
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PROVIDER_VALIDATE_RESPONSE' || data.type === 'SETTINGS_PROVIDER_VALIDATE_RESULT') {
          const provider = data.payload.provider;
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success' }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Test Successful' }));
            // Auto-clear success message after 3 seconds
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
        // ignore JSON parse errors
      }
    };
    
    wsRef.current.addEventListener('message', handleMessage);
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  useEffect(() => {
    // Initialize local state with backend keys
    const initKeys = {};
    const initBaseUrls = {};
    if (Array.isArray(providers)) {
      providers.forEach(p => {
        // We don't receive the actual API key for security, so we leave it empty.
        // But we do receive the base_url.
        initBaseUrls[p.name] = p.base_url || '';
      });
    }
    setKeys(prev => ({...initKeys, ...prev}));
    setBaseUrls(prev => ({...initBaseUrls, ...prev}));
  }, [providers]);

  const handleSaveKey = (providerName) => {
    let finalProviderName = providerName;
    let displayName = undefined;
    
    if (providerName === 'custom_openai') {
      const name = customDisplayNames[providerName];
      if (!name) {
        setValidationState(prev => ({ ...prev, [providerName]: 'error' }));
        setValidationMsg(prev => ({ ...prev, [providerName]: 'Display Name is required.' }));
        return;
      }
      finalProviderName = `custom_${name.toLowerCase().replace(/[^a-z0-9]/g, '_')}`;
      displayName = name;
    }

    setValidationState(prev => ({ ...prev, [providerName]: 'saving' }));
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PROVIDER_SAVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: finalProviderName,
          api_key: keys[providerName] || '',
          base_url: baseUrls[providerName] || '',
          ...(displayName && { display_name: displayName, provider_type: 'openai_compatible' })
        }
      }));
    }
  };

  const handleTestKey = (providerName) => {
    setValidationState(prev => ({ ...prev, [providerName]: 'testing' }));
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SETTINGS_PROVIDER_VALIDATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: providerName,
          api_key: keys[providerName] || '',
          base_url: baseUrls[providerName] || ''
        }
      }));
    }
  };

  const handleDisconnect = (e, providerName) => {
    e.stopPropagation(); // prevent accordion from expanding/collapsing
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PROVIDER_REMOVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: providerName
        }
      }));
      setExpandedProvider(null);
    }
  };



  const [showLlamaPrompt, setShowLlamaPrompt] = useState(false);
  const [downloadingLlama, setDownloadingLlama] = useState(false);

  const handleDownloadLocalModel = () => {
    if (!downloadInput) return;
    
    // Check if we have llama.cpp installed (mock check for demo)
    const hasLlamaCpp = localStorage.getItem('has_llama_cpp') === 'true';
    
    if (!hasLlamaCpp) {
      setShowLlamaPrompt(true);
      return;
    }
    
    startModelDownload();
  };

  const startModelDownload = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'LOCAL_MODEL_DOWNLOAD_START',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { repo_id: downloadInput }
      }));
      setDownloadInput('');
    }
  };

  const handleInstallLlama = () => {
    setDownloadingLlama(true);
    // Simulate downloading native dependencies
    setTimeout(() => {
      localStorage.setItem('has_llama_cpp', 'true');
      setDownloadingLlama(false);
      setShowLlamaPrompt(false);
      startModelDownload();
    }, 3000);
  };

  const menuItems = [
    { id: 'General', icon: <Monitor size={18} />, label: 'General' },
    { id: 'Providers', icon: <Key size={18} />, label: 'API Providers' },
    { id: 'Models', icon: <Cpu size={18} />, label: 'Models & Tasks' },
    { id: 'Agents', icon: <Network size={18} />, label: 'Agents' },
    { id: 'Plugins', icon: <Plug size={18} />, label: 'Plugins' },
    { id: 'Memory', icon: <Database size={18} />, label: 'Memory & Storage' },
    { id: 'Capabilities', icon: <Activity size={18} />, label: 'Capabilities' },
    { id: 'Advanced', icon: <Sliders size={18} />, label: 'Advanced' }
  ];

  return (
    <div style={{ display: 'flex', height: '100%', gap: '32px' }}>
      
      {/* Settings Sidebar */}
      <div style={{
        width: '240px',
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        borderRight: '1px solid rgba(255,255,255,0.08)',
        paddingRight: '24px'
      }}>
        {menuItems.map(item => (
          <div
            key={item.id}
            onClick={() => setActiveMenu(item.id)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '12px',
              padding: '12px 16px',
              borderRadius: '12px',
              cursor: 'pointer',
              color: activeMenu === item.id ? '#fff' : '#9090a0',
              background: activeMenu === item.id ? 'rgba(255, 255, 255, 0.1)' : 'transparent',
              transition: 'all 0.2s',
              fontWeight: activeMenu === item.id ? '500' : '400'
            }}
            onMouseEnter={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
                e.currentTarget.style.color = '#fff';
              }
            }}
            onMouseLeave={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = '#9090a0';
              }
            }}
          >
            {item.icon}
            {item.label}
          </div>
        ))}
      </div>

      {/* Settings Content Area */}
      <div style={{ flex: 1, overflowY: 'auto', paddingRight: '16px' }} className="chat-scroll">
        
        {activeMenu === 'General' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '24px', fontWeight: 500 }}>General Settings</h2>
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '16px', padding: '24px' }}>
               <h3 style={{ marginBottom: '16px', fontSize: '16px' }}>Appearance</h3>
               <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '16px' }}>Customize the look and feel of Aether-OS.</p>
               {/* Controls will go here */}
               <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'system')} className="ios-glass" style={{ padding: '8px 16px', color: '#fff', border: '1px solid rgba(124, 58, 237, 0.5)', background: 'rgba(124, 58, 237, 0.2)', cursor: 'pointer' }}>System Default</button>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'dark')} className="ios-glass" style={{ padding: '8px 16px', color: '#9090a0', cursor: 'pointer' }}>Dark Mode</button>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'light')} className="ios-glass" style={{ padding: '8px 16px', color: '#9090a0', cursor: 'pointer' }}>Light Mode</button>
               </div>
            </div>
          </div>
        )}

        {activeMenu === 'Providers' && (
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
              
              {/* Spacer div to fix Chromium bottom padding scroll clipping issue */}
              <div style={{ height: '64px', flexShrink: 0 }} />
            </div>
          </div>
        )}

        {activeMenu === 'Models' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Models & Tasks</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Assign default models globally, or route specific tasks to specialized models.</p>
            
            {/* Toggle Switch */}
            <div style={{ display: 'flex', background: 'rgba(255,255,255,0.05)', padding: '4px', borderRadius: '12px', marginBottom: '24px', width: 'fit-content' }}>
               <button 
                  onClick={() => setModelTab('cloud')}
                  style={{ background: modelTab === 'cloud' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                  Cloud Models
               </button>
               <button 
                  onClick={() => setModelTab('local')}
                  style={{ background: modelTab === 'local' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                  Local Models
               </button>
            </div>

            {modelTab === 'cloud' && (
               <>
                 <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
                   <div style={{ display: 'flex', gap: '24px' }}>
                     <div style={{ flex: 1 }}>
                       <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Global Default Model</h3>
                       <Dropdown 
                         value={
                           (Array.isArray(providers) ? providers : []).find(p => p.is_default)
                             ? `${(Array.isArray(providers) ? providers : []).find(p => p.is_default).name}:::${(Array.isArray(providers) ? providers : []).find(p => p.is_default).default_model}`
                             : ''
                         }
                         onChange={(val) => {
                           const [provider, model] = val.split(':::');
                           if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                             wsRef.current.send(JSON.stringify({
                               type: 'MODEL_SET_DEFAULT',
                               schema_version: 1,
                               request_id: Date.now().toString(),
                               payload: { provider, model }
                             }));
                           }
                         }}
                         options={
                           (Array.isArray(providers) ? providers : [])
                             .filter(p => p.has_key && p.models && p.models.length > 0)
                             .map(p => ({
                               group: p.display_name || p.name,
                               items: p.models.map(m => ({ value: `${p.name}:::${m}`, label: m }))
                             }))
                         }
                         placeholder="Select a default model..."
                         style={{ width: '100%' }}
                       />
                     </div>
                     <div style={{ width: '200px' }}>
                       <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Reasoning Effort</h3>
                       <Dropdown 
                         value="standard"
                         onChange={() => {}}
                         options={[
                           { value: 'standard', label: 'Standard' },
                           { value: 'medium', label: 'Medium' },
                           { value: 'high', label: 'Deep Thinking' }
                         ]}
                         style={{ width: '100%' }}
                       />
                     </div>
                   </div>
                 </div>

                 <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '32px', marginBottom: '16px' }}>
                   <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>⚙ Auxiliary models</h3>
                   <button style={{ background: 'transparent', color: '#a78bfa', border: 'none', fontSize: '13px', cursor: 'pointer' }}>Reset all to main</button>
                 </div>
                 <p style={{ color: '#9090a0', fontSize: '13px', marginBottom: '16px' }}>Helper tasks run on the main model by default. Assign a dedicated model to any task to override.</p>
                 
                 <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                   {[
                     { id: 'vision', name: 'Vision', desc: 'Image analysis' },
                     { id: 'web', name: 'Web extract', desc: 'Page summarization' },
                     { id: 'compression', name: 'Compression', desc: 'Context compaction' },
                     { id: 'skills', name: 'Skills hub', desc: 'Skill search' },
                     { id: 'approval', name: 'Approval', desc: 'Smart auto-approve' },
                     { id: 'mcp', name: 'MCP', desc: 'MCP tool routing' },
                     { id: 'title', name: 'Title gen', desc: 'Session titles' }
                   ].map(task => (
                     <div key={task.id} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>{task.name}</h4>
                            <span style={{ fontSize: '11px', color: '#9090a0' }}>{task.desc}</span>
                          </div>
                          <p style={{ fontSize: '12px', color: '#a78bfa', marginTop: '4px', fontFamily: 'monospace' }}>auto · use main model</p>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <Dropdown 
                            value="auto"
                            onChange={(val) => {
                              const [provider, model] = val.split(':::');
                              if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                                wsRef.current.send(JSON.stringify({
                                  type: 'AUXILIARY_MODEL_SET_REQUEST',
                                  schema_version: 1,
                                  request_id: Date.now().toString(),
                                  payload: { task: task.id, provider, model: model || 'auto' }
                                }));
                              }
                            }}
                            options={[
                              { value: 'auto', label: 'Use main model' },
                              ...(Array.isArray(providers) ? providers : [])
                                .filter(p => p.has_key && p.models && p.models.length > 0)
                                .map(p => ({
                                  group: p.display_name || p.name,
                                  items: p.models.map(m => ({ value: `${p.name}:::${m}`, label: m }))
                                }))
                            ]}
                            style={{ width: '180px' }}
                            align="right"
                          />
                        </div>
                     </div>
                   ))}
                 </div>
               </>
            )}

            {modelTab === 'local' && (
               <>
                 <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
                   <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Download GGUF Model (HuggingFace)</h3>
                   <div style={{ display: 'flex', gap: '12px', marginBottom: '16px' }}>
                     <input 
                       type="text"
                       value={downloadInput}
                       onChange={(e) => setDownloadInput(e.target.value)}
                       className="ios-glass-input" 
                       placeholder="e.g. TheBloke/Llama-2-7B-Chat-GGUF" 
                       style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                     />
                     <button 
                       onClick={handleDownloadLocalModel}
                       style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '0 24px', borderRadius: '12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 500 }}
                     >
                       <Download size={16} />
                       Download
                     </button>
                   </div>
                   
                   {showLlamaPrompt && (
                     <div style={{ background: 'rgba(239, 68, 68, 0.1)', border: '1px solid rgba(239, 68, 68, 0.2)', borderRadius: '12px', padding: '16px', marginTop: '16px' }}>
                       <h4 style={{ color: '#fca5a5', margin: '0 0 8px 0', fontSize: '14px' }}>Native Inference Engine Required</h4>
                       <p style={{ color: '#e2e8f0', fontSize: '13px', margin: '0 0 16px 0' }}>
                         To run local models, you need to download the native `llama.cpp` inference engine (~25MB). Would you like to download it now?
                       </p>
                       <div style={{ display: 'flex', gap: '12px' }}>
                         <button 
                           onClick={handleInstallLlama}
                           disabled={downloadingLlama}
                           style={{ background: '#ef4444', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }}
                         >
                           {downloadingLlama ? 'Downloading...' : 'Install Native Engine'}
                         </button>
                         <button 
                           onClick={() => setShowLlamaPrompt(false)}
                           disabled={downloadingLlama}
                           style={{ background: 'rgba(255,255,255,0.1)', color: '#fff', border: 'none', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                         >
                           Cancel
                         </button>
                       </div>
                     </div>
                   )}
                 </div>

                 <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Available Local Models</h3>
                 <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                   {localModels.length === 0 ? (
                     <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                       No local models detected. Ensure Ollama is running.
                     </div>
                   ) : (
                     localModels.map(lm => (
                       <div key={lm.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                         <div>
                           <h4 style={{ fontSize: '15px', fontWeight: 500 }}>{lm.name}</h4>
                           <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>Size: {(lm.size / (1024*1024*1024)).toFixed(1)} GB</p>
                         </div>
                       </div>
                     ))
                   )}
                 </div>
               </>
            )}
          </div>
        )}

        {activeMenu === 'Agents' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Agents (LangGraph Nodes)</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Assign specialized models to individual agent nodes in the Aether execution graph.</p>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
               {/* Mocking the fetched graph nodes for now */}
               {['Planner Agent', 'Execution Worker', 'Reviewer', 'Tool Executor'].map(node => (
                 <div key={node} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(124, 58, 237, 0.2)', borderRadius: '16px', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                   <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                     <div style={{ background: 'rgba(124,58,237,0.2)', padding: '10px', borderRadius: '10px', color: '#a78bfa' }}>
                        <Box size={24} />
                     </div>
                     <div>
                       <h3 style={{ fontSize: '16px', marginBottom: '4px' }}>{node}</h3>
                       <p style={{ color: '#9090a0', fontSize: '13px' }}>Aether-OS Backend Node</p>
                     </div>
                   </div>
                   <Dropdown 
                     value="inherit"
                     onChange={() => {}}
                     options={[
                       { value: 'inherit', label: '[Use Global Default Model]' },
                       { value: 'gpt-4o', label: 'GPT-4o (OpenAI)' },
                       { value: 'claude-3-5-sonnet', label: 'Claude 3.5 Sonnet' },
                       { value: 'llama3', label: 'llama3 (Local)' }
                     ]}
                     style={{ width: '220px' }}
                     align="right"
                   />
                 </div>
               ))}
               <div style={{ textAlign: 'center', color: '#9090a0', marginTop: '16px', fontSize: '13px' }}>
                 Auto-fetching live nodes from LangGraph backend...
               </div>
            </div>
          </div>
        )}

        {activeMenu === 'Capabilities' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Engine Capabilities</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Monitor the health and capabilities of the Aether execution environment.</p>
            
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: '#10b981', boxShadow: '0 0 10px rgba(16,185,129,0.5)' }}></div>
                  <h3 style={{ fontSize: '16px' }}>System Healthy</h3>
                </div>
                <button style={{ background: 'transparent', color: '#7c3aed', border: '1px solid rgba(124, 58, 237, 0.5)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer' }}>
                  Run Diagnostics Now
                </button>
              </div>
              
              <div style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '12px', padding: '16px', fontFamily: 'monospace', fontSize: '13px', color: '#10b981', height: '200px', overflowY: 'auto' }} className="chat-scroll">
                <div style={{ color: '#9090a0', marginBottom: '8px' }}>[13:30:45] Initiating engine capability checks...</div>
                <div>[13:30:46] Python Environment: OK (v3.12.0)</div>
                <div>[13:30:46] Terminal Access: OK (Permissions granted)</div>
                <div>[13:30:47] SQLite Storage: OK (Read/Write verified)</div>
                <div style={{ color: '#fbbf24' }}>[13:30:48] Ollama Local Service: FAILED (Connection refused)</div>
                <div>[13:30:49] Web Search API: OK (Key verified)</div>
                <div style={{ color: '#9090a0', marginTop: '8px' }}>[13:30:50] Diagnostics complete.</div>
              </div>
            </div>
          </div>
        )}

        {activeMenu === 'Plugins' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Plugins</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage active plugins and extensions for Aether-OS.</p>
            <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: '#9090a0', marginTop: '64px' }}>
              <h3>Plugin manager coming soon...</h3>
            </div>
          </div>
        )}

        {activeMenu === 'Memory' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Memory & Storage</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage the internal vector database and SQLite session storage.</p>
            <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: '#9090a0', marginTop: '64px' }}>
              <h3>Storage manager coming soon...</h3>
            </div>
          </div>
        )}

        {activeMenu === 'Advanced' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Advanced Settings</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>System configurations and developer options.</p>
            <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: '#9090a0', marginTop: '64px' }}>
              <h3>Advanced options coming soon...</h3>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
