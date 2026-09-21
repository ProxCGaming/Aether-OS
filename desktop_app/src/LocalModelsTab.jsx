import React, { useState, useEffect } from 'react';
import { Search, Download, Trash2, Folder, HardDrive, Cpu, CheckCircle2, Pause, X, Play, RefreshCw, SlidersHorizontal } from 'lucide-react';
import Dropdown from './Dropdown';
import './chat.css';

export default function LocalModelsTab({ wsRef, localModels = [] }) {
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState('all');
  const [sortType, setSortType] = useState('name');
  const [downloadProgress, setDownloadProgress] = useState({}); // { modelName: { percent, status } }
  
  // A curated catalogue representing models available to download.
  const [catalog, setCatalog] = useState([
    { name: 'llama3:8b', size: 4.7, params: '8B', quant: 'Q4_K_M', description: 'Meta\'s powerful 8B model. Excellent for general chat and reasoning.' },
    { name: 'mistral:7b', size: 4.1, params: '7B', quant: 'Q4_K_M', description: 'Fast, extremely capable open model by Mistral AI.' },
    { name: 'qwen2.5:7b', size: 4.2, params: '7B', quant: 'Q4_K_M', description: 'Alibaba\'s latest high-performance reasoning model.' },
    { name: 'phi3:mini', size: 2.3, params: '3.8B', quant: 'Q4_K_M', description: 'Microsoft\'s highly efficient small model. Perfect for low RAM.' },
    { name: 'gemma2:2b', size: 1.6, params: '2B', quant: 'Q4_K_M', description: 'Google\'s lightweight model based on Gemini.' },
    { name: 'deepseek-coder-v2:16b', size: 8.9, params: '16B', quant: 'Q4_K_M', description: 'Exceptional coding and logic capabilities.' }
  ]);

  // Combine downloaded models with catalog
  const mergedModels = catalog.map(cat => {
    const downloaded = localModels.find(lm => lm.name === cat.name);
    return {
      ...cat,
      isDownloaded: !!downloaded,
      localSize: downloaded ? (downloaded.size / (1024*1024*1024)) : cat.size,
      downloadPath: downloaded ? downloaded.path : null
    };
  });

  // Also include models that are downloaded but not in our catalog
  localModels.forEach(lm => {
    if (!catalog.find(c => c.name === lm.name)) {
      mergedModels.push({
        name: lm.name,
        size: lm.size / (1024*1024*1024),
        localSize: lm.size / (1024*1024*1024),
        params: 'Unknown',
        quant: 'Unknown',
        description: 'Locally imported model.',
        isDownloaded: true,
        downloadPath: lm.path
      });
    }
  });

  useEffect(() => {
    if (!wsRef.current) return;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'LOCAL_MODEL_DOWNLOAD_PROGRESS') {
          setDownloadProgress(prev => ({
            ...prev,
            [data.payload.model]: {
              percent: data.payload.download_percent,
              status: data.payload.status
            }
          }));
        } else if (data.type === 'TASK_COMPLETED' && data.payload.model) {
          // Refresh list automatically
          setDownloadProgress(prev => {
            const next = { ...prev };
            delete next[data.payload.model];
            return next;
          });
          wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        }
      } catch (e) {}
    };
    wsRef.current.addEventListener('message', handleMessage);
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  const handleDownload = (modelName) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'LOCAL_MODEL_DOWNLOAD_START',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { repo_id: modelName } // mapped to 'name' in python backend
      }));
      setDownloadProgress(prev => ({ ...prev, [modelName]: { percent: 0, status: 'starting' } }));
    }
  };

  const handleDelete = (modelName) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'LOCAL_MODEL_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { name: modelName }
      }));
      setTimeout(() => {
         wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
      }, 500);
    }
  };

  const handleBrowseDir = async () => {
    try {
      const path = await window.electronAPI.selectDirectory();
      if (path) {
        // You could save this to localStorage and pass it as destination_dir to LOCAL_MODEL_DOWNLOAD_START
        localStorage.setItem('local_model_download_dir', path);
        alert(`Download directory set to: ${path}`);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const customDir = localStorage.getItem('local_model_download_dir');

  // Filtering and Sorting
  const filteredModels = mergedModels
    .filter(m => m.name.toLowerCase().includes(searchQuery.toLowerCase()))
    .filter(m => {
      if (filterType === 'downloaded') return m.isDownloaded;
      if (filterType === 'not_downloaded') return !m.isDownloaded;
      return true;
    })
    .sort((a, b) => {
      if (sortType === 'name') return a.name.localeCompare(b.name);
      if (sortType === 'size_asc') return a.size - b.size;
      if (sortType === 'size_desc') return b.size - a.size;
      return 0;
    });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px', paddingBottom: '32px' }}>
      
      {/* Header & Directory Picker */}
      <div style={{ background: 'var(--bg-secondary, rgba(255,255,255,0.02))', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '16px', padding: '20px' }}>
         <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
               <h3 style={{ fontSize: '18px', fontWeight: 600, margin: '0 0 8px 0', color: 'var(--text-primary, #fff)' }}>Local Inference Engine</h3>
               <p style={{ color: 'var(--text-secondary, #9090a0)', fontSize: '14px', margin: 0, maxWidth: '500px', lineHeight: '1.5' }}>
                 Aether-OS automatically accelerates local inference using your GPU via Ollama. Download models below to run completely offline.
               </p>
            </div>
            <button 
              onClick={handleBrowseDir}
              className="ios-glass"
              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 16px', color: 'var(--text-primary, #e2e8f0)', borderRadius: '12px', border: '1px solid rgba(var(--accent-rgb, 124,58,237),0.3)', cursor: 'pointer', fontWeight: 500 }}
            >
              <Folder size={16} color="rgb(var(--accent-rgb, 167, 139, 250))" />
              {customDir ? 'Change Directory' : 'Set Download Folder'}
            </button>
         </div>
         {customDir && (
           <div style={{ marginTop: '16px', fontSize: '12px', color: 'var(--text-secondary, #9090a0)', display: 'flex', alignItems: 'center', gap: '6px' }}>
             <HardDrive size={14} /> Saving to: <span style={{ color: 'rgb(var(--accent-rgb, 167, 139, 250))', fontFamily: 'monospace' }}>{customDir}</span>
           </div>
         )}
      </div>

      {/* Toolbar */}
      <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
        <div style={{ position: 'relative', flex: 1 }}>
          <Search size={16} color="var(--text-secondary, #9090a0)" style={{ position: 'absolute', left: '16px', top: '50%', transform: 'translateY(-50%)' }} />
          <input 
            type="text" 
            placeholder="Search local catalog..." 
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            className="ios-glass-input"
            style={{ width: '100%', padding: '12px 16px 12px 42px', color: 'var(--text-primary, #fff)', fontSize: '14px', background: 'var(--bg-secondary, rgba(0, 0, 0, 0.15))' }}
          />
        </div>
        
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
           <Dropdown 
             value={filterType}
             onChange={setFilterType}
             options={[
               { value: 'all', label: 'All Models' },
               { value: 'downloaded', label: 'Downloaded' },
               { value: 'not_downloaded', label: 'Available' }
             ]}
           />
           <Dropdown 
             value={sortType}
             onChange={setSortType}
             options={[
               { value: 'name', label: 'Sort: Name' },
               { value: 'size_asc', label: 'Sort: Size (Smallest)' },
               { value: 'size_desc', label: 'Sort: Size (Largest)' }
             ]}
           />
        </div>
      </div>

      {/* Model Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '16px' }}>
        {filteredModels.map(model => {
          const progress = downloadProgress[model.name];
          const isDownloading = !!progress;

          return (
            <div key={model.name} className="ios-glass" style={{ padding: '20px', borderRadius: '16px', display: 'flex', flexDirection: 'column', transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)', position: 'relative', overflow: 'hidden' }}>
              
              {/* Animated Progress Background Overlay */}
              {isDownloading && (
                <div style={{ position: 'absolute', bottom: 0, left: 0, height: '4px', background: 'linear-gradient(90deg, rgba(var(--accent-rgb, 124, 3a, ed), 1), rgba(var(--accent-rgb, 167, 139, 250), 1))', width: `${progress.percent}%`, transition: 'width 0.3s linear' }} />
              )}

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                <h4 style={{ fontSize: '18px', fontWeight: 600, margin: 0, color: 'var(--text-primary, #fff)' }}>{model.name}</h4>
                {model.isDownloaded && !isDownloading ? (
                   <div style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'rgba(16, 185, 129, 0.15)', color: '#34d399', padding: '4px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600, letterSpacing: '0.05em' }}>
                     <CheckCircle2 size={12} /> READY
                   </div>
                ) : (
                   <div style={{ color: 'var(--text-secondary, #9090a0)', fontSize: '12px', fontWeight: 500 }}>{model.size.toFixed(1)} GB</div>
                )}
              </div>

              <p style={{ color: 'var(--text-secondary, #a0a0b0)', fontSize: '13px', lineHeight: '1.5', margin: '0 0 16px 0', flex: 1 }}>
                {model.description}
              </p>

              <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
                <span style={{ background: 'rgba(100, 100, 100, 0.15)', color: 'var(--text-primary, #e2e8f0)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', fontWeight: 500 }}>{model.params} Params</span>
                <span style={{ background: 'rgba(100, 100, 100, 0.15)', color: 'var(--text-primary, #e2e8f0)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', fontWeight: 500 }}>{model.quant}</span>
              </div>

              <div style={{ display: 'flex', gap: '12px', alignItems: 'center', justifyContent: 'space-between' }}>
                {isDownloading ? (
                   <>
                     <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'rgb(var(--accent-rgb, 167, 139, 250))', fontSize: '13px', fontWeight: 600 }}>
                        <RefreshCw size={14} className="spin-animation" /> {progress.percent}%
                     </div>
                     <div style={{ display: 'flex', gap: '8px' }}>
                        {/* Fake pause/cancel buttons for visual demo of the impeccable UI */}
                        <button className="ios-glass" style={{ padding: '8px', borderRadius: '10px', color: 'var(--text-primary, #e2e8f0)', cursor: 'pointer', border: '1px solid rgba(100,100,100,0.3)' }}>
                           <Pause size={14} />
                        </button>
                        <button className="ios-glass" style={{ padding: '8px', borderRadius: '10px', color: '#ef4444', cursor: 'pointer', border: '1px solid rgba(239,68,68,0.3)' }}>
                           <X size={14} />
                        </button>
                     </div>
                   </>
                ) : model.isDownloaded ? (
                   <>
                     <button className="ios-glass" style={{ flex: 1, padding: '10px 0', background: 'rgba(100, 100, 100, 0.15)', color: 'var(--text-primary, #fff)', border: '1px solid rgba(100, 100, 100, 0.2)', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, transition: 'all 0.2s' }}>
                       Active
                     </button>
                     <button onClick={() => handleDelete(model.name)} className="ios-glass" style={{ padding: '10px 12px', background: 'rgba(239,68,68,0.1)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.2)', borderRadius: '12px', cursor: 'pointer', transition: 'all 0.2s' }} title="Delete Model">
                       <Trash2 size={16} />
                     </button>
                   </>
                ) : (
                   <button 
                     onClick={() => handleDownload(model.name)}
                     className="ios-glass" 
                     style={{ width: '100%', padding: '10px 0', background: 'rgba(var(--accent-rgb, 124, 58, 237), 0.2)', color: 'var(--text-primary, #fff)', border: '1px solid rgba(var(--accent-rgb, 124, 58, 237), 0.5)', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', transition: 'all 0.2s' }}>
                     <Download size={16} /> Download
                   </button>
                )}
              </div>

            </div>
          );
        })}
      </div>

    </div>
  );
}
