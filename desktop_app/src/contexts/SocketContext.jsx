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

// Wave 2 slice 1: a descriptor for one message sitting in the offline
// queue. The queue stores serialized envelopes, so the descriptor is
// recovered by parsing the entry. Envelope messages (safeSend and the
// component sends) carry their arguments under `payload`; flat
// sendMessage traffic spreads its fields at the top level, so the
// remaining fields stand in as the payload. Unparseable entries (none
// are produced today) describe as a null type rather than throwing —
// the count is the load-bearing part.
function describeQueuedMessage(data) {
  try {
    const parsed = JSON.parse(data);
    if (!parsed || typeof parsed !== 'object') return { type: null, payload: null };
    if (parsed.payload !== undefined) {
      return { type: parsed.type ?? null, payload: parsed.payload };
    }
    const { type, ...rest } = parsed;
    return { type: type ?? null, payload: rest };
  } catch (e) {
    return { type: null, payload: null };
  }
}

// Wave 2 slice 4 (audit Lane A, QUEUE-class caution): the destructive
// actions that must NOT flush verbatim when the socket reconnects.
// Everything else in the offline queue is safe to land late — a queued
// fetch or edit arriving minutes after the click is what the user
// still wants — but these four can land after the user has
// context-switched, changed their mind, or started new work, so a
// queued copy is held for explicit reconfirmation instead (see
// heldMessages / confirmHeld / discardHeld on the provider value).
// The set is defined ONCE, here, so no call site decides for itself
// what counts as destructive:
//
//   CONFIG_RESET_REQUEST        — reset every engine setting to
//                                 defaults (SettingsPanel).
//   MEMORY_EPISODES_CLEAR_REQUEST / MEMORY_GRAPH_CLEAR_REQUEST
//                               — MemoryManagement's clear-all actions
//                                 (episodes and the knowledge graph).
//   PROVIDER_REMOVE_REQUEST     — remove a provider and its stored
//                                 key (ProviderSettings disconnect).
//   CANCEL_TASK                 — cancel the running task. A stale
//                                 cancel is held on purpose: by the
//                                 time it would flush, the task it
//                                 meant to stop may be long finished
//                                 and a NEW task — started after the
//                                 reconnect — could be the one it
//                                 kills.
export const DESTRUCTIVE_MESSAGE_TYPES = new Set([
  'CONFIG_RESET_REQUEST',
  'MEMORY_EPISODES_CLEAR_REQUEST',
  'MEMORY_GRAPH_CLEAR_REQUEST',
  'PROVIDER_REMOVE_REQUEST',
  'CANCEL_TASK',
]);

// Plain-language label for one held message descriptor (the shape
// describeQueuedMessage produces), used by the reconfirmation prompt.
// Where the payload carries a name — the provider being removed —
// the label names it, so the user confirms the exact action.
export function destructiveActionLabel(descriptor) {
  const type = descriptor?.type ?? null;
  const payload = descriptor?.payload ?? {};
  switch (type) {
    case 'CONFIG_RESET_REQUEST':
      return 'Reset all settings to defaults';
    case 'MEMORY_EPISODES_CLEAR_REQUEST':
      return 'Clear all memory episodes';
    case 'MEMORY_GRAPH_CLEAR_REQUEST':
      return 'Clear the whole memory graph';
    case 'PROVIDER_REMOVE_REQUEST':
      return payload.provider ? `Remove provider “${payload.provider}”` : 'Remove a provider';
    case 'CANCEL_TASK':
      return 'Cancel the running task';
    default:
      return type ? `Send ${type}` : 'Send a queued action';
  }
}

export const SocketProvider = ({ children }) => {
  const [isConnected, setIsConnected] = useState(false);
  const [pendingMessages, setPendingMessages] = useState([]);
  const [heldMessages, setHeldMessages] = useState([]);
  const [reconnectCount, setReconnectCount] = useState(0);
  const messageQueue = useRef([]);
  const heldQueueRef = useRef([]);
  const sawDisconnectRef = useRef(false);
  const statusRef = useRef(false);
  const wsListeners = useRef(new Map());
  const wsRef = useRef(null);

  // Wave 2 slice 1: the offline queue is a ref (the facade below is
  // built once, outside React's render cycle), so its contents are
  // mirrored into state — pendingMessages / pendingCount are the
  // reactive view the UI reads (the app-level pending indicator, a
  // control waiting on a queued action). syncPending runs after every
  // queue mutation: facade queue-push, sendMessage queue-push, and the
  // reconnect flush. Defined before the facade because the facade's
  // send() calls it; it only touches the stable queue ref and the
  // stable state setter, so the first-render closure stays correct.
  const syncPending = useCallback(() => {
    setPendingMessages(messageQueue.current.map(describeQueuedMessage));
  }, []);

  // Wave 2 slice 4: the same mirror for the held set — destructive
  // messages diverted out of the flush by checkQueue live in
  // heldQueueRef until the user confirms or discards them.
  const syncHeld = useCallback(() => {
    setHeldMessages(heldQueueRef.current.map(describeQueuedMessage));
  }, []);

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
          return 'sent';
        }
        messageQueue.current.push(data);
        syncPending();
        return 'queued';
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
      if (!statusRef.current) return;
      const stranded = [];
      while (messageQueue.current.length > 0) {
        const msg = messageQueue.current.shift();
        const { type } = describeQueuedMessage(msg);
        if (type && DESTRUCTIVE_MESSAGE_TYPES.has(type)) {
          // Wave 2 slice 4: destructive actions never flush
          // automatically. They move — in original queue order — to
          // the held set, where they stay until the user confirms or
          // discards them (confirmHeld / discardHeld), across as
          // many further reconnects as happen in between.
          heldQueueRef.current.push(msg);
        } else if (window.electronAPI && window.electronAPI.sendEngineMessage) {
          window.electronAPI.sendEngineMessage(msg);
        } else {
          stranded.push(msg);
        }
      }
      if (stranded.length > 0) {
        // No transport to flush through (no electronAPI bridge):
        // keep the messages queued, in order, rather than dropping
        // them on the floor.
        messageQueue.current.unshift(...stranded);
      }
      syncPending();
      syncHeld();
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
              // Wave 2 slice 4: reconnectCount is the refetch signal
              // for fetch-once effects (RUX-A-16/A-17). It counts
              // only real recoveries — a disconnected period followed
              // by a connect — never the initial connect, whose
              // mounts run their own first fetch anyway.
              if (sawDisconnectRef.current) {
                sawDisconnectRef.current = false;
                setReconnectCount((count) => count + 1);
              }
              checkQueue();
            } else {
              statusRef.current = false;
              setIsConnected(false);
              sawDisconnectRef.current = true;
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
        if (status) {
          if (sawDisconnectRef.current) {
            sawDisconnectRef.current = false;
            setReconnectCount((count) => count + 1);
          }
          checkQueue();
        } else {
          sawDisconnectRef.current = true;
        }
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
      syncPending();
    }
  }, [syncPending]);

  // safeSend(type, payload) — the send path for USER ACTIONS: anything
  // a person deliberately did in the UI (a model switch, a settings
  // edit, an approval decision, a destructive command). It builds the
  // standard component envelope ({ type, schema_version, request_id,
  // payload }) the call sites used to hand-roll, hands it to the
  // facade, and reports what actually happened:
  //
  //   'sent'   — the engine connection is live; the message is gone.
  //   'queued' — the connection is down; the message is in the offline
  //              queue and will flush, in order, on reconnect.
  //
  // A user action sent through safeSend is NEVER silently dropped —
  // that guarantee is the whole point. The old failure mode (Lane A of
  // the runtime/UX audit) was a readyState guard in front of the send
  // throwing the action away while the socket was down, before the
  // facade's queue could ever see it. Callers that get 'queued' should
  // surface it (the app-level pending indicator tracks the queue via
  // pendingCount / pendingMessages; a control whose value only changes
  // on the engine's reply should additionally show its own pending
  // affordance, as the Settings model dropdown does).
  //
  // Do NOT use safeSend for engine-internal traffic: response chains
  // fired from inside message handlers, fetch-on-open requests, and
  // sends that must keep an existing non-envelope wire shape stay on
  // sendMessage / wsRef.send (audit class RUX-A-18 — those paths are
  // not user actions, and several self-heal via reconnect effects).
  //
  // The optional requestId exists for the rare user action whose
  // envelope id is a CORRELATION id the engine answers against —
  // plugin approvals are the case in point: the engine parks the
  // pending install under the PLUGIN_APPROVAL_REQUEST's request_id
  // and pops it by the decision's request_id, so a facade-minted id
  // would orphan the install. Pass the id the engine is waiting for;
  // every other caller leaves it null and gets a minted one.
  const safeSend = useCallback((type, payload = {}, requestId = null) => {
    const data = JSON.stringify({
      type,
      schema_version: 1,
      request_id: requestId !== null && requestId !== undefined ? String(requestId) : Date.now().toString(),
      payload,
    });
    return wsRef.current.send(data);
  }, []);

  // Wave 2 slice 4: the user's verdict on held destructive actions.
  // confirmHeld releases held messages in their original queue order
  // — all of them, or one by its heldMessages index. If the socket
  // has dropped again by the time the user decides, the released
  // messages go back to the FRONT of the offline queue (ahead of
  // anything queued since): on the next reconnect they are held
  // again, never flushed. discardHeld drops held messages
  // permanently — all, or one by index. Neither ever runs on its own:
  // a held message the user ignores stays held indefinitely.
  const confirmHeld = useCallback((index = null) => {
    let release;
    if (index === null || index === undefined) {
      release = heldQueueRef.current;
      heldQueueRef.current = [];
    } else {
      if (index < 0 || index >= heldQueueRef.current.length) return;
      release = [heldQueueRef.current[index]];
      heldQueueRef.current = heldQueueRef.current.filter((_, i) => i !== index);
    }
    syncHeld();
    if (release.length === 0) return;
    if (statusRef.current && window.electronAPI && window.electronAPI.sendEngineMessage) {
      release.forEach((msg) => window.electronAPI.sendEngineMessage(msg));
    } else {
      messageQueue.current = [...release, ...messageQueue.current];
      syncPending();
    }
  }, [syncHeld, syncPending]);

  const discardHeld = useCallback((index = null) => {
    if (index === null || index === undefined) {
      heldQueueRef.current = [];
    } else {
      heldQueueRef.current = heldQueueRef.current.filter((_, i) => i !== index);
    }
    syncHeld();
  }, [syncHeld]);

  return (
    <SocketContext.Provider
      value={{
        isConnected,
        sendMessage,
        safeSend,
        wsRef,
        pendingCount: pendingMessages.length,
        pendingMessages,
        heldMessages,
        heldCount: heldMessages.length,
        confirmHeld,
        discardHeld,
        reconnectCount,
      }}
    >
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
