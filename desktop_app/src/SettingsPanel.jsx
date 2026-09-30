import React, { useState, useEffect } from 'react';
import { User, Key, Cpu, Settings as SettingsIcon, Monitor, Activity, Download, Network, Box, Layers, Plug, Database, Sliders, CheckCircle2, Wrench, Server, Trash2 } from 'lucide-react';
import Dropdown from './Dropdown';
import LocalModelsTab from './LocalModelsTab';
import './chat.css';
import ProviderSettings from './components/Settings/ProviderSettings';
import MemoryManagement from './components/Settings/MemoryManagement';
import DiagnosticsPanel from './components/Settings/DiagnosticsPanel';

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
  
  const [skills, setSkills] = useState([]);
  
  // Advanced Config State
  const [disableFallbacks, setDisableFallbacks] = useState(false);
  const [fallbackChain, setFallbackChain] = useState('');
  
  // A5: State for Reasoning Effort and Node model override
  const [reasoningEffort, setReasoningEffort] = useState(() => localStorage.getItem('reasoning_effort') || 'standard');
  const [agents, setAgents] = useState([]);

  const [memoryTab, setMemoryTab] = useState('episodic');
  const [episodes, setEpisodes] = useState([]);
  const [facts, setFacts] = useState([]);
  const [selectedEpisode, setSelectedEpisode] = useState(null);
  
  const [downloadInput, setDownloadInput] = useState('');
  
  const [capabilityHistory, setCapabilityHistory] = useState([]);
  const [capabilityStatus, setCapabilityStatus] = useState(null);
  const [isDiagnosticsRunning, setIsDiagnosticsRunning] = useState(false);

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
        } else if (data.type === 'AGENT_LIST_RESPONSE') {
          setAgents(data.payload.agents || []);
        } else if (data.type === 'AGENT_UPDATE_RESPONSE') {
          // Can optionally show a toast here
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
        } else if (data.type === 'SKILL_LIST_RESPONSE') {
          setSkills(data.payload.skills || []);
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
        } else if (data.type === 'ENVIRONMENT_DIAGNOSTIC_RESPONSE') {
          setCapabilityStatus(data.payload.summary);
          setCapabilityHistory(data.payload.history || []);
          setIsDiagnosticsRunning(false);
        } else if (data.type === 'CONFIG_GET_RESPONSE' || data.type === 'CONFIG_UPDATE_RESPONSE') {
          if (data.payload.config) {
            setDisableFallbacks(data.payload.config.disable_fallbacks || false);
            setFallbackChain((data.payload.config.fallback_chain || []).join(', '));
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
    if (activeMenu === 'Tools' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TOOL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Agents' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'AGENT_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      // Also fetch tools for the agent tool selector
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
    } else if (activeMenu === 'Skills' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SKILL_LIST_REQUEST',
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
    } else if (activeMenu === 'Advanced' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_GET_REQUEST',
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
    { id: 'Skills', icon: <Box size={18} />, label: 'Skills' },
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
      <div style={{ flex: 1, overflowY: 'auto', paddingRight: '16px', paddingBottom: '80px' }} className="chat-scroll">
        
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
          <ProviderSettings providers={providers} />
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
          <DiagnosticsPanel activeMenu='Agents' />
        )}

        {activeMenu === 'Capabilities' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Engine Capabilities</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Monitor the health and capabilities of the Aether execution environment.</p>
            
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: capabilityStatus && capabilityStatus.failed_count === 0 ? '#10b981' : (capabilityStatus && capabilityStatus.failed_count > 0 ? '#ef4444' : '#6b7280'), boxShadow: capabilityStatus && capabilityStatus.failed_count === 0 ? '0 0 10px rgba(16,185,129,0.5)' : 'none' }}></div>
                  <h3 style={{ fontSize: '16px' }}>{capabilityStatus ? (capabilityStatus.failed_count === 0 ? 'System Healthy' : `${capabilityStatus.failed_count} Issues Detected`) : 'Status Unknown'}</h3>
                </div>
                <button 
                  onClick={() => {
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && !isDiagnosticsRunning) {
                      setIsDiagnosticsRunning(true);
                      wsRef.current.send(JSON.stringify({
                        type: 'ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: {}
                      }));
                    }
                  }}
                  disabled={isDiagnosticsRunning}
                  style={{ 
                    background: isDiagnosticsRunning ? 'rgba(124, 58, 237, 0.2)' : 'transparent', 
                    color: isDiagnosticsRunning ? '#a78bfa' : '#7c3aed', 
                    border: '1px solid rgba(124, 58, 237, 0.5)', 
                    padding: '8px 16px', 
                    borderRadius: '8px', 
                    cursor: isDiagnosticsRunning ? 'wait' : 'pointer',
                    transition: 'all 0.2s ease',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px'
                  }}>
                  {isDiagnosticsRunning ? (
                    <>
                      <svg style={{ width: '16px', height: '16px', animation: 'spin 1s linear infinite' }} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle style={{ opacity: 0.25 }} cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                        <path style={{ opacity: 0.75 }} fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Testing...
                    </>
                  ) : (
                    'Run Diagnostics Now'
                  )}
                </button>
              </div>
              
              <div style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '12px', padding: '16px', fontFamily: 'monospace', fontSize: '13px', color: '#10b981', height: '200px', overflowY: 'auto' }} className="chat-scroll">
                {capabilityHistory.length === 0 ? (
                  <div style={{ color: '#9090a0', marginBottom: '8px' }}>[System] Ready to run diagnostics. Click the button above.</div>
                ) : (
                  capabilityHistory.map((log, idx) => (
                    <div key={idx} style={{ color: log.status === 'OK' || log.status === 'RUNNING' ? '#10b981' : (log.status === 'WARN' ? '#fbbf24' : '#ef4444') }}>
                      [{new Date(log.timestamp * 1000).toLocaleTimeString()}] {log.target_name}: {log.status} {log.details ? `(${log.details})` : ''}
                    </div>
                  ))
                )}
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

        {activeMenu === 'MCP' && (
          <DiagnosticsPanel activeMenu='MCP' />
        )}

        {activeMenu === 'Plugins' && (
          <DiagnosticsPanel activeMenu='Plugins' />
        )}

        {activeMenu === 'Skills' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>
              Dynamic behaviors, prompts, and tool requirements loaded from `.agents/skills`. Trigger these in chat with slash commands.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {skills.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No skills found in workspace. Create a skill in <code>.agents/skills/</code>.
                </div>
              ) : (
                skills.map(skill => (
                  <div key={skill.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                      <div>
                        <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {skill.name}
                          <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>
                        </h4>
                        <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                          {skill.description}
                        </p>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                      {skill.slash_commands?.map(cmd => (
                        <span key={cmd} style={{ fontSize: '12px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', padding: '4px 8px', borderRadius: '6px' }}>
                          {cmd}
                        </span>
                      ))}
                      {skill.required_tools?.length > 0 && (
                        <span style={{ fontSize: '12px', background: 'rgba(251, 191, 36, 0.15)', color: '#fbbf24', padding: '4px 8px', borderRadius: '6px' }}>
                          {skill.required_tools.length} Tools Required
                        </span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeMenu === 'Memory' && (
          <MemoryManagement />
        )}

        {activeMenu === 'Advanced' && (
          <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '24px' }}>
            <div>
              <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Advanced Settings</h2>
              <p style={{ color: '#9090a0', fontSize: '14px' }}>System configurations and developer options.</p>
            </div>
            
            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '16px', color: '#fff', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Network size={18} style={{ color: '#a78bfa' }} /> Model Routing & Fallbacks
              </h3>
              
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '4px' }}>Disable Fallback System</div>
                  <div style={{ fontSize: '12px', color: '#9090a0' }}>Prevent the system from automatically trying alternative models when the current one fails.</div>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={disableFallbacks}
                    onChange={(e) => {
                      const val = e.target.checked;
                      setDisableFallbacks(val);
                      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                        wsRef.current.send(JSON.stringify({
                          type: 'CONFIG_UPDATE_REQUEST',
                          schema_version: 1,
                          request_id: Date.now().toString(),
                          payload: { config: { disable_fallbacks: val } }
                        }));
                      }
                    }}
                    style={{ accentColor: '#7c3aed', width: '18px', height: '18px' }}
                  />
                </label>
              </div>

              {!disableFallbacks && (
                <div>
                  <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px' }}>Fallback Provider Chain (Optional)</div>
                  <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
                    Comma-separated list of providers to try in order (e.g., <code>openrouter, groq, google_gemini</code>). Leave empty for automatic selection.
                  </div>
                  <input
                    type="text"
                    value={fallbackChain}
                    onChange={(e) => setFallbackChain(e.target.value)}
                    onBlur={() => {
                      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                        const parsedChain = fallbackChain.split(',').map(s => s.trim()).filter(Boolean);
                        wsRef.current.send(JSON.stringify({
                          type: 'CONFIG_UPDATE_REQUEST',
                          schema_version: 1,
                          request_id: Date.now().toString(),
                          payload: { config: { fallback_chain: parsedChain } }
                        }));
                      }
                    }}
                    placeholder="E.g. openrouter, groq, anthropic"
                    style={{
                      width: '100%',
                      background: 'rgba(0,0,0,0.2)',
                      border: '1px solid rgba(255,255,255,0.1)',
                      borderRadius: '6px',
                      padding: '10px 12px',
                      color: '#fff',
                      fontSize: '13px'
                    }}
                  />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
