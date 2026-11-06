import React, { useState } from 'react';
import Dropdown from '../../Dropdown';
import LocalModelsTab from '../../LocalModelsTab';
import LoadStateRow from './LoadStateRow';
import SpeechSettingsPanel from './SpeechSettingsPanel';

// Wave 5 (Annex C row 7): the Models & Tasks tab, extracted from
// SettingsPanel.jsx — the shell keeps the config state, the one
// message handler and the sends; this panel is the controlled
// rendering (the SpeechSettingsPanel convention: state and named
// handlers arrive as props). The cloud/local toggle is this
// tab's own state and lives here.
export default function ModelsPanel({ providers, wsRef, safeSend, pendingMessages, reconnectCount, isConnected, configAnswered, configLoadError, onRetryConfigLoad, modelSwitchQueued, pendingModelSwitch, onModelDefaultChange, reasoningEffort, onReasoningEffortChange, controlRefusal, auxModels, onAuxModelChange, onAuxResetAll, engineConfig, configUpdateError, onConfigUpdate }) {
  const [modelTab, setModelTab] = useState('cloud'); // 'cloud' or 'local'
  return (
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
           {/* RUX-C-01: config-driven controls on this tab
               (reasoning effort, auxiliary models, speech)
               show engine defaults until the first config
               answer lands — the tab says so until then. */}
           {!configAnswered && (
             <div style={{ marginBottom: '24px' }}>
               <LoadStateRow
                 loadState={configLoadError ? 'error' : 'loading'}
                 isConnected={isConnected}
                 testId="models-config-load-state"
                 variant="plain"
                 loadingText="Loading your saved model settings…"
                 offlineText="Waiting for the engine — your saved model settings will appear here once it connects."
                 errorText={configLoadError || 'Your saved model settings could not be loaded.'}
               />
               {configLoadError && (
                 <button type="button" onClick={onRetryConfigLoad} style={{ marginTop: '8px', background: 'transparent', color: '#a78bfa', border: '1px solid rgba(124,58,237,0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}>
                   Retry
                 </button>
               )}
             </div>
           )}
           <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
             <div style={{ display: 'flex', gap: '24px' }}>
               <div style={{ flex: 1 }}>
                 <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Global Default Model</h3>
                 <div style={{ opacity: modelSwitchQueued ? 0.55 : 1, pointerEvents: modelSwitchQueued ? 'none' : 'auto' }}>
                 <Dropdown
                   value={
                     (Array.isArray(providers) ? providers : []).find(p => p.is_default)
                       ? `${(Array.isArray(providers) ? providers : []).find(p => p.is_default).name}:::${(Array.isArray(providers) ? providers : []).find(p => p.is_default).default_model}`
                       : ''
                   }
                   onChange={onModelDefaultChange}
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
                 {modelSwitchQueued && (
                   <div style={{ fontSize: '12px', color: '#fbbf24', marginTop: '8px' }}>
                     Model switch{pendingModelSwitch.payload && pendingModelSwitch.payload.model ? ` to ${pendingModelSwitch.payload.model}` : ''} queued — will apply when the engine reconnects.
                   </div>
                 )}
               </div>
               <div style={{ width: '200px' }}>
                 <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Reasoning Effort</h3>
                 <Dropdown 
                   value={reasoningEffort}
                   onChange={onReasoningEffortChange}
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

           {controlRefusal && (
             <div style={{ color: '#f87171', fontSize: '13px', marginTop: '16px' }}>{controlRefusal}</div>
           )}

           <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '32px', marginBottom: '16px' }}>
             <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>⚙ Auxiliary models</h3>
             <button
               style={{ background: 'transparent', color: '#a78bfa', border: 'none', fontSize: '13px', cursor: 'pointer' }}
               onClick={onAuxResetAll}
             >Reset all to main</button>
           </div>
           <p style={{ color: '#9090a0', fontSize: '13px', marginBottom: '16px' }}>Helper tasks run on the main model by default. Assign a dedicated model to any task to override.</p>
                 
           <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
             {[
               // Phase 9, B1 (UI honesty): only tasks with a real engine seam are
               // shown. Vision overrides are honored by the routing policy; the
               // other helper tasks have no engine call site yet, so no control
               // is rendered for them instead of a placebo dropdown.
               { id: 'vision', name: 'Vision', desc: 'Image analysis' },
             ].map(task => (
               <div key={task.id} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>{task.name}</h4>
                      <span style={{ fontSize: '11px', color: '#9090a0' }}>{task.desc}</span>
                    </div>
                    <p style={{ fontSize: '12px', color: '#a78bfa', marginTop: '4px', fontFamily: 'monospace' }}>{auxModels[task.id] ? auxModels[task.id].split(':::')[1] || auxModels[task.id] : 'auto · use main model'}</p>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Dropdown 
                      value={auxModels[task.id] || 'auto'}
                      onChange={(val) => onAuxModelChange(task.id, val)}
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

           {/* Model Hub H5: the Speech card — speech model,
               voice and transcription model, controlled from
               the applied config like every handed-off surface
               (E6-F05) and written through sendConfigUpdate. */}
           <SpeechSettingsPanel
             config={engineConfig}
             configError={configUpdateError}
             onConfigUpdate={onConfigUpdate}
             pendingMessages={pendingMessages}
             reconnectCount={reconnectCount}
             configReady={configAnswered}
             wsRef={wsRef}
           />
         </>
      )}

      {modelTab === 'local' && (
         <LocalModelsTab
           wsRef={wsRef}
           safeSend={safeSend}
           pendingMessages={pendingMessages}
           // Phase 10.3 C2: the active model lives on the providers
           // array (the provider flagged is_default); LocalModelsTab
           // needs it to show which downloaded model is Active.
           defaultProvider={(Array.isArray(providers) ? providers : []).find(p => p.is_default)?.name || ''}
           defaultModel={(Array.isArray(providers) ? providers : []).find(p => p.is_default)?.default_model || ''}
           // The configured embedder lives on the applied
           // config; the tab marks that downloaded row as the
           // active Embedder.
           currentEmbedder={engineConfig?.embedding_model || ''}
         />
      )}
    </div>
  );
}
