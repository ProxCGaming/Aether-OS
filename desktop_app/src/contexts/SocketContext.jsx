import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react';

const SocketContext = createContext(null);

export const SocketProvider = ({ children }) => {
  const [isConnected, setIsConnected] = useState(false);
  const messageQueue = useRef([]);
  const wsRef = useRef(null);

  useEffect(() => {
    let unsubs = [];
    let currentStatus = false;
    
    const wsListeners = new Map();

    const checkQueue = () => {
      if (currentStatus) {
        while (messageQueue.current.length > 0) {
          const msg = messageQueue.current.shift();
          if (window.electronAPI && window.electronAPI.sendEngineMessage) {
            window.electronAPI.sendEngineMessage(msg);
          }
        }
      }
    };

    const mockWs = {
      get readyState() {
        return currentStatus ? WebSocket.OPEN : WebSocket.CLOSED;
      },
      send: (data) => {
        if (currentStatus && window.electronAPI && window.electronAPI.sendEngineMessage) {
          window.electronAPI.sendEngineMessage(data);
        } else {
          messageQueue.current.push(data);
        }
      },
      addEventListener: (event, handler) => {
        if (event === 'message' && window.electronAPI && window.electronAPI.onEngineMessage) {
          const cleanup = window.electronAPI.onEngineMessage((data) => {
            handler({ data });
          });
          wsListeners.set(handler, cleanup);
        }
      },
      removeEventListener: (event, handler) => {
        if (event === 'message' && wsListeners.has(handler)) {
          const cleanup = wsListeners.get(handler);
          if (typeof cleanup === 'function') cleanup();
          wsListeners.delete(handler);
        }
      }
    };

    wsRef.current = mockWs;

    if (window.electronAPI && window.electronAPI.onEngineMessage) {
      const unsub = window.electronAPI.onEngineMessage((data) => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.type === '_ws_status') {
            if (parsed.status === 'connected') {
              currentStatus = true;
              setIsConnected(true);
              checkQueue();
            } else {
              currentStatus = false;
              setIsConnected(false);
            }
          }
        } catch (e) {
          // generic message
        }
      });
      unsubs.push(unsub);
    }

    if (window.electronAPI && window.electronAPI.getWsStatus) {
      window.electronAPI.getWsStatus().then((status) => {
        currentStatus = status;
        setIsConnected(status);
        if (status) checkQueue();
      });
    }

    return () => {
      unsubs.forEach(fn => fn && fn());
      wsListeners.forEach(cleanup => {
        if (typeof cleanup === 'function') cleanup();
      });
      wsListeners.clear();
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
