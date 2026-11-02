import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react';

const SocketContext = createContext(null);

// Phase 10.3 C1: the single dedup point for the whole renderer. This
// replaces the module-level 150ms hack that lived in App.jsx: it only
// guarded App's own handler while every other subscriber got duplicates,
// and it papered over the real cause (App's async initConnection leaked its
// first subscription under React StrictMode double-mount, so each packet
// arrived twice). Dedup is per subscriber, not global: two different
// subscribers must each receive one copy of the same packet.
//
// S3-M9: suppression is keyed on the packet's own IDENTITY, never on raw
// content alone. The old rule ("same string as the previous packet within
// 150ms") dropped legitimate packets: two distinct stream packets can
// carry byte-identical content, and content equality is not duplication.
// Engine packets are Event JSON (aether_common/contracts.py): a top-level
// `request_id`/`event_id` mirrored pair that names the TASK (every chunk
// of one stream shares it — keying on it alone would eat the stream) and
// a per-instance `ts` stamped when the Event was constructed. A genuine
// duplicate — the leaked subscription redelivering the SAME serialized
// instance — repeats identity (id + ts) and bytes exactly, so the dedup
// key is that identity tuple. Distinct instances always differ in id or
// ts and are always delivered. A packet with neither an identity field
// nor a ts carries nothing that could distinguish a duplicate delivery
// from a distinct packet, so it is never suppressed: guessing from its
// content is exactly the defect being fixed.
const DEDUP_WINDOW_MS = 150;

// The dedup key for one raw engine packet, or null when the packet has
// no identity to key on (unparseable, or neither id nor ts present).
function packetIdentityKey(data) {
  let parsed;
  try {
    parsed = JSON.parse(data);
  } catch (e) {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const id = parsed.event_id ?? parsed.request_id ?? null;
  const ts = parsed.ts ?? null;
  if (id === null && ts === null) return null;
  // The exact bytes ride along so two instances that share an id and a
  // ts but differ in content still count as distinct packets; only a
  // byte-identical redelivery of an already-delivered identity is a
  // duplicate.
  return `${id}|${ts}|${parsed.type ?? ''}|${data}`;
}

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
          // Packet identities delivered to THIS subscriber within the
          // retention window (identity key -> delivery time). Bounded by
          // the window: entries are pruned on every packet, and a repeat
          // of an identity after the window is a new delivery, not a
          // duplicate.
          const deliveredIdentities = new Map();
          const cleanup = window.electronAPI.onEngineMessage((data) => {
            const key = packetIdentityKey(data);
            if (key !== null) {
              const now = Date.now();
              for (const [seenKey, seenAt] of deliveredIdentities) {
                if ((now - seenAt) >= DEDUP_WINDOW_MS) {
                  deliveredIdentities.delete(seenKey);
                }
              }
              if (deliveredIdentities.has(key)) {
                return; // true duplicate: same packet identity redelivered
              }
              deliveredIdentities.set(key, now);
            }
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
