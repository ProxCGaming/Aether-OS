import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react';

const SocketContext = createContext(null);

// Phase 10.3 C1: the single dedup point for the whole renderer. An identical
// IPC packet arriving within this window is delivered once per subscriber.
// This replaces the module-level 150ms hack that lived in App.jsx: it only
// guarded App's own handler while every other subscriber got duplicates,
// and it papered over the real cause (App's async initConnection leaked its
// first subscription under React StrictMode double-mount, so each packet
// arrived twice). Dedup is per subscriber, not global: two different
// subscribers must each receive one copy of the same packet.
const DEDUP_WINDOW_MS = 150;

export const SocketProvider = ({ children }) => {
  const [isConnected, setIsConnected] = useState(false);
  const messageQueue = useRef([]);
  const statusRef = useRef(false);
  const wsListeners = useRef(new Map());
  const wsRef = useRef(null);

  // Lazy-init during render (the documented exception to "don't write refs
  // during render"): AppContent mounts at the same time as this provider
  // and its mount effect runs BEFORE this provider's effect (child effects
  // first), so the facade must already exist when children render. The
  // object itself performs no IPC until addEventListener/send is called.
  if (wsRef.current === null) {
    const mockWs = {
      get readyState() {
        return statusRef.current ? WebSocket.OPEN : WebSocket.CLOSED;
      },
      send: (data) => {
        if (statusRef.current && window.electronAPI && window.electronAPI.sendEngineMessage) {
          window.electronAPI.sendEngineMessage(data);
        } else {
          messageQueue.current.push(data);
        }
      },
      addEventListener: (event, handler) => {
        if (event === 'message' && window.electronAPI && window.electronAPI.onEngineMessage) {
          let lastData = null;
          let lastTime = 0;
          const cleanup = window.electronAPI.onEngineMessage((data) => {
            const now = Date.now();
            if (data === lastData && (now - lastTime) < DEDUP_WINDOW_MS) {
              return; // identical duplicate packet within the window
            }
            lastData = data;
            lastTime = now;
            handler({ data });
          });
          wsListeners.current.set(handler, cleanup);
        }
      },
      removeEventListener: (event, handler) => {
        if (event === 'message' && wsListeners.current.has(handler)) {
          const cleanup = wsListeners.current.get(handler);
          if (typeof cleanup === 'function') cleanup();
          wsListeners.current.delete(handler);
        }
      }
    };

    wsRef.current = mockWs;
  }

  useEffect(() => {
    const checkQueue = () => {
      if (statusRef.current) {
        while (messageQueue.current.length > 0) {
          const msg = messageQueue.current.shift();
          if (window.electronAPI && window.electronAPI.sendEngineMessage) {
            window.electronAPI.sendEngineMessage(msg);
          }
        }
      }
    };

    let unsub = null;
    if (window.electronAPI && window.electronAPI.onEngineMessage) {
      unsub = window.electronAPI.onEngineMessage((data) => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.type === '_ws_status') {
            if (parsed.status === 'connected') {
              statusRef.current = true;
              setIsConnected(true);
              checkQueue();
            } else {
              statusRef.current = false;
              setIsConnected(false);
            }
          }
        } catch (e) {
          // generic message
        }
      });
    }

    if (window.electronAPI && window.electronAPI.getWsStatus) {
      window.electronAPI.getWsStatus().then((status) => {
        statusRef.current = status;
        setIsConnected(status);
        if (status) checkQueue();
      });
    }

    return () => {
      if (typeof unsub === 'function') unsub();
      wsListeners.current.forEach(cleanup => {
        if (typeof cleanup === 'function') cleanup();
      });
      wsListeners.current.clear();
    };
  }, []);

  const sendMessage = useCallback((type, payload = {}) => {
    const data = JSON.stringify({ type, ...payload });
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(data);
    } else {
      messageQueue.current.push(data);
    }
  }, []);

  return (
    <SocketContext.Provider value={{ isConnected, sendMessage, wsRef }}>
      {children}
    </SocketContext.Provider>
  );
};

export const useAetherSocket = () => {
  const context = useContext(SocketContext);
  if (!context) {
    throw new Error('useAetherSocket must be used within a SocketProvider');
  }
  return context;
};
