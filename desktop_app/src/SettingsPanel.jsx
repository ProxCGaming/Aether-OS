import React, { useState, useEffect } from 'react';
import { User, Key, Cpu, Settings as SettingsIcon, Monitor, Activity, Download, Network, Box, Layers, Plug, Database, Sliders, CheckCircle2, Wrench, Server, Trash2 } from 'lucide-react';
import Dropdown from './Dropdown';
import LocalModelsTab from './LocalModelsTab';
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
  
  const [tools, setTools] = useState([]);
  const [mcpServers, setMcpServers] = useState({});
  const [newMcpServer, setNewMcpServer] = useState({ name: '', command: '', args: '' });

  const [plugins, setPlugins] = useState([]);
  const [newPluginSource, setNewPluginSource] = useState('');
  
  // A5: State for Reasoning Effort and Node model override
  const [reasoningEffort, setReasoningEffort] = useState(() => localStorage.getItem('reasoning_effort') || 'standard');
  const [nodeModels, setNodeModels] = useState(() => JSON.parse(localStorage.getItem('node_models') || '{}'));

  const [memoryTab, setMemoryTab] = useState('episodic');
  const [episodes, setEpisodes] = useState([]);
  const [facts, setFacts] = useState([]);
  const [selectedEpisode, setSelectedEpisode] = useState(null);
  
  const [downloadInput, setDownloadInput] = useState('');

  const handleDeleteEpisode = (taskId) => {
    if (!taskId) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_EPISODE_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { task_id: taskId }
      }));
    }
    setEpisodes(prev => prev.filter(ep => ep.task_id !== taskId));
    if (selectedEpisode?.task_id === taskId) {
      setSelectedEpisode(null);
    }
  };

  const handleClearAllEpisodes = () => {
    if (window.confirm("Are you sure you want to clear all stored episodic memories?")) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_EPISODES_CLEAR_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: {}
        }));
      }
      setEpisodes([]);
      setSelectedEpisode(null);
    }
  };

  const handleDeleteFact = (factId) => {
    if (!factId) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_FACT_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { fact_id: factId }
      }));
    }
    setFacts(prev => prev.filter(f => f.fact_id !== factId));
  };

  const handleDeleteEntity = (entityName) => {
    if (!entityName) return;
    if (window.confirm(`Delete all facts associated with "${entityName}"?`)) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_ENTITY_DELETE_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { entity_name: entityName }
        }));
      }
      setFacts(prev => prev.filter(f => f.entity_name !== entityName));
    }
  };

  const handleClearAllFacts = () => {
    if (window.confirm("Are you sure you want to clear all facts from the Knowledge Graph?")) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_GRAPH_CLEAR_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: {}
        }));
      }
      setFacts([]);
    }
  };

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
        } else if (data.type === 'TOOL_LIST_RESPONSE') {
          setTools(data.payload.tools || []);
        } else if (data.type === 'TOOL_POLICY_SET_RESPONSE') {
          if (data.payload.success) {
            setTools(prev => prev.map(t => t.function.name === data.payload.tool_name ? { ...t, policy: data.payload.policy } : t));
          }
        } else if (data.type === 'MCP_SERVER_LIST_RESPONSE') {
          setMcpServers(data.payload.servers || {});
        } else if (data.type === 'MCP_SERVER_ADD_RESPONSE' || data.type === 'MCP_SERVER_REMOVE_RESPONSE') {
          if (data.payload.success) {
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: 'MCP_SERVER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          }
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const p = data.payload.plugins;
          setPlugins(Array.isArray(p) ? p : Object.values(p || {}));
        } else if (data.type === 'PLUGIN_INSTALL_RESPONSE' || data.type === 'PLUGIN_UNINSTALL_RESPONSE' || data.type === 'PLUGIN_TOGGLE_RESPONSE') {
          if (data.payload.success) {
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: 'PLUGIN_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          }
        } else if (data.type === 'MEMORY_EPISODES_RESPONSE' || data.type === 'MEMORY_EPISODE_DELETE_RESPONSE') {
          setEpisodes(data.payload.episodes || []);
        } else if (data.type === 'MEMORY_EPISODES_CLEAR_RESPONSE') {
          setEpisodes([]);
          setSelectedEpisode(null);
        } else if (data.type === 'MEMORY_GRAPH_RESPONSE' || data.type === 'MEMORY_FACT_DELETE_RESPONSE' || data.type === 'MEMORY_ENTITY_DELETE_RESPONSE') {
          setFacts(data.payload.facts || []);
        } else if (data.type === 'MEMORY_GRAPH_CLEAR_RESPONSE') {
          setFacts([]);
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
    if (activeMenu === 'Tools' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TOOL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'MCP' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MCP_SERVER_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Plugins' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PLUGIN_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Memory' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_EPISODES_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_GRAPH_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
  }, [activeMenu, wsRef]);

  useEffect(() => {
    // Initialize local state with backend keys
    const initKeys = {};
    const initBaseUrls = {};
    if (Array.isArray(providers)) {
      providers.forEach(p => {
        // We don't receive the actual API key for security, so we leave it empty.
        // But we can pre-fill it from localStorage if the user previously entered it!
        const savedLocalKey = localStorage.getItem(`aether_saved_key_${p.name}`);
        if (savedLocalKey) {
          initKeys[p.name] = savedLocalKey;
        }
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
      const apiKeyToSave = keys[providerName] || '';
      if (apiKeyToSave) {
        localStorage.setItem(`aether_saved_key_${finalProviderName}`, apiKeyToSave);
      }
      wsRef.current.send(JSON.stringify({
        type: 'PROVIDER_SAVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: finalProviderName,
          api_key: apiKeyToSave,
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
    { id: 'Tools', icon: <Wrench size={18} />, label: 'Tools' },
    { id: 'MCP', icon: <Server size={18} />, label: 'MCP Servers' },
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
               <div style={{ display: 'flex', alignItems: 'center', gap: '8px', background: 'rgba(0,0,0,0.2)', padding: '6px', borderRadius: '16px', width: 'fit-content' }}>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'default')} className="ios-glass" style={{ padding: '8px 20px', color: '#fff', border: '1px solid rgba(var(--accent-rgb, 124, 58, 237), 0.5)', background: 'rgba(var(--accent-rgb, 124, 58, 237), 0.2)', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, transition: 'all 0.2s' }}>Default</button>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'system')} className="ios-glass" style={{ padding: '8px 20px', color: '#9090a0', background: 'transparent', border: '1px solid transparent', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, transition: 'all 0.2s' }}>System Default</button>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'light')} className="ios-glass" style={{ padding: '8px 20px', color: '#9090a0', background: 'transparent', border: '1px solid transparent', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, transition: 'all 0.2s' }}>Light</button>
                  <button onClick={() => document.documentElement.setAttribute('data-theme', 'dark')} className="ios-glass" style={{ padding: '8px 20px', color: '#9090a0', background: 'transparent', border: '1px solid transparent', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, transition: 'all 0.2s' }}>Dark</button>
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
                         value={reasoningEffort}
                         onChange={(val) => {
                           setReasoningEffort(val);
                           localStorage.setItem('reasoning_effort', val);
                         }}
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
               <LocalModelsTab wsRef={wsRef} localModels={localModels} />
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
                     value={nodeModels[node] || 'inherit'}
                     onChange={(val) => {
                       const next = { ...nodeModels, [node]: val };
                       setNodeModels(next);
                       localStorage.setItem('node_models', JSON.stringify(next));
                     }}
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

        { activeMenu === 'Tools' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Tools Sandbox</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Configure auto-approval policies for built-in tools.</p>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {tools.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No tools registered.
                </div>
              ) : (
                tools.map(tool => (
                  <div key={tool.function.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <h4 style={{ fontSize: '15px', fontWeight: 500 }}>{tool.function.name}</h4>
                      <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>{tool.function.description}</p>
                    </div>
                    <div>
                      <Dropdown 
                        value={tool.policy || 'Require Approval'}
                        onChange={(val) => {
                          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                            wsRef.current.send(JSON.stringify({
                              type: 'TOOL_POLICY_SET_REQUEST',
                              schema_version: 1,
                              request_id: Date.now().toString(),
                              payload: { tool_name: tool.function.name, policy: val }
                            }));
                          }
                        }}
                        options={[
                          { value: 'Always Allow', label: 'Always Allow' },
                          { value: 'Require Approval', label: 'Require Approval' },
                          { value: 'Deny', label: 'Deny' }
                        ]}
                        style={{ width: '160px' }}
                        align="right"
                      />
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        { activeMenu === 'MCP' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>MCP Connections</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage remote Model Context Protocol servers.</p>
            
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
              <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Add New Server</h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <input 
                  type="text"
                  value={newMcpServer.name}
                  onChange={(e) => setNewMcpServer({...newMcpServer, name: e.target.value})}
                  className="ios-glass-input" 
                  placeholder="Server Name (e.g. SQLite DB)" 
                  style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <input 
                  type="text"
                  value={newMcpServer.command}
                  onChange={(e) => setNewMcpServer({...newMcpServer, command: e.target.value})}
                  className="ios-glass-input" 
                  placeholder="Command (e.g. python, npx)" 
                  style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <input 
                  type="text"
                  value={newMcpServer.args}
                  onChange={(e) => setNewMcpServer({...newMcpServer, args: e.target.value})}
                  className="ios-glass-input" 
                  placeholder="Arguments (comma separated)" 
                  style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <button 
                  onClick={() => {
                    if (newMcpServer.name && newMcpServer.command && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'MCP_SERVER_ADD_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: { 
                          name: newMcpServer.name, 
                          config: { 
                            command: newMcpServer.command, 
                            args: newMcpServer.args.split(',').map(s => s.trim()).filter(Boolean) 
                          }
                        }
                      }));
                      setNewMcpServer({ name: '', command: '', args: '' });
                    }
                  }}
                  style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '12px 24px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, alignSelf: 'flex-start' }}
                >
                  Add Server
                </button>
              </div>
            </div>

            <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Configured Servers</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {Object.keys(mcpServers).length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No MCP servers configured.
                </div>
              ) : (
                Object.entries(mcpServers).map(([name, config]) => (
                  <div key={name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <h4 style={{ fontSize: '15px', fontWeight: 500 }}>{name}</h4>
                      <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px', fontFamily: 'monospace' }}>
                        {config.command} {config.args?.join(' ')}
                      </p>
                    </div>
                    <div>
                      <button 
                        onClick={() => {
                          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                            wsRef.current.send(JSON.stringify({
                              type: 'MCP_SERVER_REMOVE_REQUEST',
                              schema_version: 1,
                              request_id: Date.now().toString(),
                              payload: { name }
                            }));
                          }
                        }}
                        style={{ background: 'rgba(242, 65, 91, 0.15)', color: '#FA5870', border: '1px solid rgba(242, 65, 91, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                      >
                        Remove
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeMenu === 'Plugins' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Plugins</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage active plugins and extensions for Aether-OS.</p>
            
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
              <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Install New Plugin</h3>
              <div style={{ display: 'flex', gap: '12px' }}>
                <input 
                  type="text"
                  value={newPluginSource}
                  onChange={(e) => setNewPluginSource(e.target.value)}
                  className="ios-glass-input" 
                  placeholder="GitHub URL or local path" 
                  style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <button 
                  onClick={() => {
                    if (newPluginSource && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'PLUGIN_INSTALL_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: { source: newPluginSource }
                      }));
                      setNewPluginSource('');
                    }
                  }}
                  style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '0 24px', borderRadius: '12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 500 }}
                >
                  <Download size={16} />
                  Install
                </button>
              </div>
            </div>

            <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Installed Plugins</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {plugins.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No plugins installed.
                </div>
              ) : (
                plugins.map(plugin => (
                  <div key={plugin.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                        {plugin.manifest?.name || plugin.name}
                        <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>v{plugin.manifest?.version || '1.0'}</span>
                      </h4>
                      <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                        {plugin.manifest?.description || 'No description available.'}
                      </p>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                      <button 
                        onClick={() => {
                          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                            wsRef.current.send(JSON.stringify({
                              type: 'PLUGIN_TOGGLE_REQUEST',
                              schema_version: 1,
                              request_id: Date.now().toString(),
                              payload: { name: plugin.name, enabled: !plugin.enabled }
                            }));
                          }
                        }}
                        style={{ background: plugin.enabled ? 'rgba(16, 185, 129, 0.15)' : 'rgba(255, 255, 255, 0.1)', color: plugin.enabled ? '#10b981' : '#fff', border: `1px solid ${plugin.enabled ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255, 255, 255, 0.2)'}`, padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', width: '80px' }}
                      >
                        {plugin.enabled ? 'Enabled' : 'Disabled'}
                      </button>
                      <button 
                        onClick={() => {
                          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                            wsRef.current.send(JSON.stringify({
                              type: 'PLUGIN_UNINSTALL_REQUEST',
                              schema_version: 1,
                              request_id: Date.now().toString(),
                              payload: { name: plugin.name }
                            }));
                          }
                        }}
                        style={{ background: 'rgba(242, 65, 91, 0.15)', color: '#FA5870', border: '1px solid rgba(242, 65, 91, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                      >
                        Uninstall
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeMenu === 'Memory' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Memory & Storage</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage the internal knowledge graph and episodic memory.</p>
            
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
              <div style={{ display: 'flex', background: 'rgba(255,255,255,0.05)', padding: '4px', borderRadius: '12px', width: 'fit-content' }}>
                 <button 
                    onClick={() => setMemoryTab('episodic')}
                    style={{ background: memoryTab === 'episodic' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                    Episodic Memory
                 </button>
                 <button 
                    onClick={() => setMemoryTab('kg')}
                    style={{ background: memoryTab === 'kg' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                    Knowledge Graph
                 </button>
              </div>
              {memoryTab === 'episodic' && episodes.length > 0 && (
                <button
                  onClick={handleClearAllEpisodes}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    background: 'rgba(239, 68, 68, 0.12)',
                    border: '1px solid rgba(239, 68, 68, 0.3)',
                    color: '#f87171',
                    padding: '8px 16px',
                    borderRadius: '10px',
                    fontSize: '13px',
                    fontWeight: 500,
                    cursor: 'pointer',
                    transition: 'all 0.2s'
                  }}
                  onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
                  onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
                >
                  <Trash2 size={15} />
                  Clear All Memory
                </button>
              )}
              {memoryTab === 'kg' && facts.length > 0 && (
                <button
                  onClick={handleClearAllFacts}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    background: 'rgba(239, 68, 68, 0.12)',
                    border: '1px solid rgba(239, 68, 68, 0.3)',
                    color: '#f87171',
                    padding: '8px 16px',
                    borderRadius: '10px',
                    fontSize: '13px',
                    fontWeight: 500,
                    cursor: 'pointer',
                    transition: 'all 0.2s'
                  }}
                  onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
                  onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
                >
                  <Trash2 size={15} />
                  Clear All Knowledge
                </button>
              )}
            </div>

            {memoryTab === 'episodic' && (
              <div style={{ display: 'flex', gap: '24px', height: '600px' }}>
                <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '8px', overflowY: 'auto', paddingRight: '8px' }} className="chat-scroll">
                  {episodes.length === 0 ? (
                    <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center' }}>No episodes found.</div>
                  ) : (
                    episodes.map(ep => (
                      <div 
                        key={ep.task_id || ep.id} 
                        onClick={() => setSelectedEpisode(ep)}
                        style={{ 
                          background: selectedEpisode?.task_id === ep.task_id ? 'rgba(124,58,237,0.2)' : 'rgba(255,255,255,0.02)', 
                          border: `1px solid ${selectedEpisode?.task_id === ep.task_id ? 'rgba(124,58,237,0.5)' : 'rgba(255,255,255,0.05)'}`, 
                          borderRadius: '12px', padding: '14px 16px', cursor: 'pointer',
                          transition: 'all 0.2s',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: '12px'
                        }}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <h4 style={{ fontSize: '14px', fontWeight: 500, margin: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {ep.user_prompt || 'System event'}
                          </h4>
                          <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '4px', margin: 0 }}>
                            {new Date(ep.timestamp * 1000).toLocaleString()}
                          </p>
                        </div>
                        <button
                          title="Delete this episode"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteEpisode(ep.task_id);
                          }}
                          style={{
                            background: 'transparent',
                            border: 'none',
                            color: '#9090a0',
                            padding: '6px',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            transition: 'all 0.2s',
                            flexShrink: 0
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.color = '#ef4444';
                            e.currentTarget.style.background = 'rgba(239, 68, 68, 0.15)';
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.color = '#9090a0';
                            e.currentTarget.style.background = 'transparent';
                          }}
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    ))
                  )}
                </div>
                <div style={{ flex: 1, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '16px', padding: '24px', overflowY: 'auto' }} className="chat-scroll">
                  {selectedEpisode ? (
                    <div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                        <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>Episode Details</h3>
                        <button
                          onClick={() => handleDeleteEpisode(selectedEpisode.task_id)}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            background: 'rgba(239, 68, 68, 0.12)',
                            border: '1px solid rgba(239, 68, 68, 0.25)',
                            color: '#f87171',
                            padding: '6px 14px',
                            borderRadius: '8px',
                            fontSize: '12px',
                            fontWeight: 500,
                            cursor: 'pointer',
                            transition: 'all 0.2s'
                          }}
                          onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
                          onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
                        >
                          <Trash2 size={14} />
                          Delete Episode
                        </button>
                      </div>
                      <div style={{ marginBottom: '16px' }}>
                        <div style={{ fontSize: '12px', color: '#a78bfa', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>User Prompt</div>
                        <div style={{ background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>
                          {selectedEpisode.user_prompt}
                        </div>
                      </div>
                      <div style={{ marginBottom: '16px' }}>
                        <div style={{ fontSize: '12px', color: '#10b981', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Response</div>
                        <div style={{ background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>
                          {selectedEpisode.outcome}
                        </div>
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                        <div>
                          <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Task ID</div>
                          <div style={{ fontSize: '13px', fontFamily: 'monospace' }}>{selectedEpisode.task_id}</div>
                        </div>
                        <div>
                          <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Timestamp</div>
                          <div style={{ fontSize: '13px' }}>{new Date(selectedEpisode.timestamp * 1000).toLocaleString()}</div>
                        </div>
                        <div>
                          <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Vector ID</div>
                          <div style={{ fontSize: '13px', fontFamily: 'monospace' }}>{selectedEpisode.vector_id || 'N/A'}</div>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', color: '#9090a0', fontSize: '14px' }}>
                      Select an episode to view details.
                    </div>
                  )}
                </div>
              </div>
            )}

            {memoryTab === 'kg' && (
              <div>
                {facts.length === 0 ? (
                  <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                    No facts in Knowledge Graph.
                  </div>
                ) : (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' }}>
                    {/* Group facts by entity_name */}
                    {Object.entries(facts.reduce((acc, f) => {
                      acc[f.entity_name] = acc[f.entity_name] || [];
                      acc[f.entity_name].push(f);
                      return acc;
                    }, {})).map(([entityName, entityFacts]) => (
                      <div key={entityName} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <h3 style={{ fontSize: '16px', fontWeight: 500, margin: 0, color: '#a78bfa' }}>{entityName}</h3>
                            <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 8px', borderRadius: '12px', color: '#9090a0' }}>
                              {entityFacts[0]?.entity_type || 'Entity'}
                            </span>
                          </div>
                          <button
                            title={`Delete all facts for ${entityName}`}
                            onClick={() => handleDeleteEntity(entityName)}
                            style={{
                              background: 'transparent',
                              border: 'none',
                              color: '#9090a0',
                              padding: '4px 6px',
                              borderRadius: '6px',
                              cursor: 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              transition: 'all 0.2s'
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.color = '#ef4444';
                              e.currentTarget.style.background = 'rgba(239, 68, 68, 0.15)';
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.color = '#9090a0';
                              e.currentTarget.style.background = 'transparent';
                            }}
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                          {entityFacts.map((fact, i) => (
                            <div 
                              key={fact.fact_id || i} 
                              style={{ 
                                display: 'flex', 
                                alignItems: 'flex-start', 
                                justifyContent: 'space-between', 
                                gap: '10px', 
                                background: 'rgba(0,0,0,0.2)', 
                                padding: '10px 12px', 
                                borderRadius: '8px', 
                                fontSize: '13px', 
                                lineHeight: '1.5' 
                              }}
                            >
                              <div style={{ flex: 1, wordBreak: 'break-word' }}>
                                {fact.fact_text}
                              </div>
                              {fact.fact_id && (
                                <button
                                  title="Delete this fact"
                                  onClick={() => handleDeleteFact(fact.fact_id)}
                                  style={{
                                    background: 'transparent',
                                    border: 'none',
                                    color: '#9090a0',
                                    padding: '4px',
                                    borderRadius: '6px',
                                    cursor: 'pointer',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    transition: 'all 0.2s',
                                    flexShrink: 0
                                  }}
                                  onMouseEnter={(e) => {
                                    e.currentTarget.style.color = '#ef4444';
                                    e.currentTarget.style.background = 'rgba(239, 68, 68, 0.15)';
                                  }}
                                  onMouseLeave={(e) => {
                                    e.currentTarget.style.color = '#9090a0';
                                    e.currentTarget.style.background = 'transparent';
                                  }}
                                >
                                  <Trash2 size={13} />
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
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
