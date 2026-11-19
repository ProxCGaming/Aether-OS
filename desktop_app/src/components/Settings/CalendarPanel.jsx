import React, { useState, useEffect, useCallback } from 'react';
import { CalendarDays, RefreshCw } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';

// Calendar layer C5 (14 Nov 2026): Settings → Calendar & Reminders.
//
// Two halves, both real:
//  - The switches. Controlled, in the E6-F05 sense: SettingsPanel
//    owns the config and the one CONFIG_UPDATE flow; this panel
//    only renders the applied values and reports edits, so a
//    refused write (configError, surfaced verbatim) simply never
//    lands and the controls keep showing what is actually in force.
//  - The diary. Self-fetched over the socket like MemoryManagement:
//    CALENDAR_LIST_REQUEST on mount, CALENDAR_LIST_RESPONSE fills
//    the list, with the shared load-state idiom (loading / offline /
//    error are distinct from a genuinely empty calendar). A
//    REMINDER_DUE push while the section is open shows a banner and
//    refreshes the list — the in-app half of the C4 reminder path
//    (the OS notification is the unfocused half).
//  - Add / cancel in-panel. The Add form calls CALENDAR_CREATE_
//    REQUEST directly (not via chat extraction); a reply carrying
//    conflicts is a warning, not a failure — nothing was saved, and
//    "Save anyway" resends the same call with accept_conflicts set.
//    Cancel confirms first (the window.confirm precedent the other
//    panels use for destructive actions) and a not-found reply is
//    shown as the failure it is, never as a success.

function formatWhen(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short',
    hour: 'numeric', minute: '2-digit',
  });
}

export default function CalendarPanel({
  configAnswered = true,
  isConnected = true,
  configError = '',
  calendarEnabled = true,
  onCalendarEnabledChange,
  calendarDayBefore = true,
  onCalendarDayBeforeChange,
  calendarLeadMinutes = 30,
  onCalendarLeadMinutesCommit,
}) {
  const { wsRef, sendMessage, isConnected: socketConnected } = useAetherSocket();
  const connected = isConnected && socketConnected !== false;
  const [events, setEvents] = useState([]);
  const [loadState, setLoadState] = useState('loading');
  const [listError, setListError] = useState('');
  const [lastReminder, setLastReminder] = useState(null);
  // null = untouched: the input shows the applied prop value. An
  // edit drafts here instead, so no effect has to chase the prop.
  const [leadDraft, setLeadDraft] = useState(null);
  const leadText = leadDraft ?? String(calendarLeadMinutes);
  // Add-form drafts + the two honest failure channels around it:
  // formError (the create call itself failed) and conflicts (the
  // create call was refused nothing — it is a warning that nothing
  // was saved yet). actionError covers cancel failures.
  const [addTitle, setAddTitle] = useState('');
  const [addStart, setAddStart] = useState('');
  const [addDurationMin, setAddDurationMin] = useState('60');
  const [formError, setFormError] = useState('');
  const [conflicts, setConflicts] = useState(null);
  const [actionError, setActionError] = useState('');

  const fetchEvents = useCallback(() => {
    sendMessage('CALENDAR_LIST_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { limit: 20 },
    });
  }, [sendMessage]);

  const submitCreate = useCallback((acceptConflicts) => {
    const title = addTitle.trim();
    if (!title) { setFormError('Give the event a title.'); return; }
    const startDate = addStart ? new Date(addStart) : null;
    if (!startDate || Number.isNaN(startDate.getTime())) {
      setFormError('Pick when the event starts.');
      return;
    }
    const minutes = Number.parseInt(addDurationMin, 10);
    if (Number.isNaN(minutes) || minutes <= 0) {
      setFormError('Give the event a duration in minutes.');
      return;
    }
    setFormError('');
    sendMessage('CALENDAR_CREATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        title,
        start: startDate.toISOString(),
        duration_s: minutes * 60,
        ...(acceptConflicts ? { accept_conflicts: true } : {}),
      },
    });
  }, [sendMessage, addTitle, addStart, addDurationMin]);

  const cancelEvent = useCallback((ev) => {
    if (!window.confirm(
      `Cancel “${ev.title}”? It stays cancelled after a restart — this is not a snooze.`,
    )) {
      return;
    }
    setActionError('');
    sendMessage('CALENDAR_CANCEL_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { event_id: ev.id },
    });
  }, [sendMessage]);

  useEffect(() => {
    fetchEvents();
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'CALENDAR_LIST_RESPONSE') {
          if (data.payload && data.payload.success === false) {
            setListError(data.payload.error || 'The calendar could not be loaded.');
            setLoadState('error');
          } else {
            setListError('');
            setEvents((data.payload && data.payload.events) || []);
            setLoadState('ready');
          }
        } else if (data.type === 'CALENDAR_CREATE_RESPONSE') {
          const payload = data.payload || {};
          if (payload.success === false || payload.error) {
            setFormError(payload.error || 'That event could not be saved.');
            setConflicts(null);
          } else if (Array.isArray(payload.conflicts) && payload.conflicts.length > 0) {
            // A warning reply, not an error: nothing was saved.
            // The form keeps its values so "Save anyway" resends
            // exactly what the owner just typed.
            setConflicts(payload.conflicts);
            setFormError('');
          } else {
            setConflicts(null);
            setFormError('');
            setAddTitle('');
            setAddStart('');
            setAddDurationMin('60');
            fetchEvents();
          }
        } else if (data.type === 'CALENDAR_CANCEL_RESPONSE') {
          const payload = data.payload || {};
          if (payload.success === false || payload.error || !payload.cancelled) {
            setActionError(payload.error || 'That event could not be cancelled.');
          } else {
            setActionError('');
            fetchEvents();
          }
        } else if (data.type === 'REMINDER_DUE') {
          setLastReminder(data.payload || null);
          fetchEvents();
        }
      } catch {
        // ignore malformed frames, same as the other panels
      }
    };
    const ws = wsRef.current;
    if (ws) ws.addEventListener('message', handleMessage);
    return () => { if (ws) ws.removeEventListener('message', handleMessage); };
  }, [wsRef, fetchEvents, connected]);

  const commitLead = () => {
    const parsed = Number.parseInt(leadText, 10);
    if (Number.isNaN(parsed)) { setLeadDraft(null); return; }
    const clamped = Math.max(0, Math.min(1440, parsed));
    setLeadDraft(null);
    if (clamped !== calendarLeadMinutes && onCalendarLeadMinutesCommit) {
      onCalendarLeadMinutesCommit(clamped);
    }
  };

  const toggleRow = (label, hint, checked, onChange, testId) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
      <div>
        <div style={{ fontSize: '14px' }}>{label}</div>
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>{hint}</div>
      </div>
      <input
        type="checkbox"
        data-testid={testId}
        checked={checked}
        onChange={(e) => onChange && onChange(e.target.checked)}
        style={{ accentColor: 'var(--accent)', width: '18px', height: '18px', flexShrink: 0 }}
      />
    </div>
  );

  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Calendar & Reminders</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
          Aether notices events you mention in chat, keeps them here, and reminds you before they start.
        </p>
      </div>

      {!configAnswered && (
        <LoadStateRow
          loadState="loading"
          isConnected={connected}
          loadingText="Loading your saved calendar settings…"
          offlineText="Waiting for the engine — your saved calendar settings will appear when it reconnects."
          testId="calendar-config-load-state"
        />
      )}

      {configAnswered && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', background: 'var(--bg-base)', borderRadius: '12px', padding: '20px' }}>
          {toggleRow(
            'Calendar',
            'Notice events in chat, include your schedule in answers, and send reminders. Stored events stay listed below either way.',
            calendarEnabled, onCalendarEnabledChange, 'calendar-enabled-toggle',
          )}
          {toggleRow(
            'Day-before reminder',
            'One reminder on the day before an event, the first time the engine checks that day.',
            calendarDayBefore, onCalendarDayBeforeChange, 'calendar-day-before-toggle',
          )}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '14px' }}>Lead reminder</div>
              <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>
                Minutes before an event starts to remind you. 0 turns the lead reminder off.
              </div>
            </div>
            <input
              type="number"
              data-testid="calendar-lead-minutes"
              min="0"
              max="1440"
              value={leadText}
              onChange={(e) => setLeadDraft(e.target.value)}
              onBlur={commitLead}
              onKeyDown={(e) => { if (e.key === 'Enter') commitLead(); }}
              style={{ width: '90px', flexShrink: 0 }}
            />
          </div>
          {configError ? (
            <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{configError}</div>
          ) : null}
        </div>
      )}

      {lastReminder && (
        <div data-testid="calendar-reminder-banner" style={{ background: 'rgba(224,160,60,0.1)', border: '1px solid rgba(224,160,60,0.35)', borderRadius: '12px', padding: '14px 18px', fontSize: '14px' }}>
          Reminder: {lastReminder.title || 'An event'}
          {lastReminder.start_text ? ` — ${lastReminder.start_text}` : ''}
        </div>
      )}

      <div style={{ background: 'var(--bg-base)', borderRadius: '12px', padding: '20px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, margin: '0 0 12px' }}>Add an event</h3>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px', alignItems: 'flex-end' }}>
          <div style={{ flex: '2 1 200px' }}>
            <label htmlFor="calendar-add-title" style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Title</label>
            <input
              id="calendar-add-title"
              data-testid="calendar-add-title"
              type="text"
              value={addTitle}
              onChange={(e) => { setAddTitle(e.target.value); setConflicts(null); }}
              placeholder="Dentist appointment"
              style={{ width: '100%', boxSizing: 'border-box' }}
            />
          </div>
          <div style={{ flex: '1 1 190px' }}>
            <label htmlFor="calendar-add-start" style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Starts</label>
            <input
              id="calendar-add-start"
              data-testid="calendar-add-start"
              type="datetime-local"
              value={addStart}
              onChange={(e) => { setAddStart(e.target.value); setConflicts(null); }}
              style={{ width: '100%', boxSizing: 'border-box' }}
            />
          </div>
          <div style={{ flex: '0 1 120px' }}>
            <label htmlFor="calendar-add-duration" style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Duration (minutes)</label>
            <input
              id="calendar-add-duration"
              data-testid="calendar-add-duration"
              type="number"
              min="1"
              value={addDurationMin}
              onChange={(e) => { setAddDurationMin(e.target.value); setConflicts(null); }}
              style={{ width: '100%', boxSizing: 'border-box' }}
            />
          </div>
          <button
            type="button"
            data-testid="calendar-add-submit"
            onClick={() => submitCreate(false)}
            style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', borderRadius: '8px', padding: '9px 18px', fontSize: '13px', fontWeight: 600, cursor: 'pointer' }}
          >
            Add event
          </button>
        </div>
        {formError ? (
          <div data-testid="calendar-add-error" style={{ color: 'var(--danger)', fontSize: '13px', marginTop: '12px' }}>{formError}</div>
        ) : null}
        {conflicts && (
          <div data-testid="calendar-conflict-warning" style={{ background: 'rgba(224,160,60,0.1)', border: '1px solid rgba(224,160,60,0.35)', borderRadius: '10px', padding: '14px 16px', marginTop: '14px' }}>
            <div style={{ fontSize: '13px', lineHeight: 1.5 }}>
              This overlaps {conflicts.length === 1 ? 'an event' : 'events'} already on your calendar:
              {' '}
              {conflicts.map((c) => `${c.title} (${formatWhen(c.start)})`).join(', ')}.
              {' '}Nothing has been saved yet.
            </div>
            <button
              type="button"
              data-testid="calendar-save-anyway"
              onClick={() => submitCreate(true)}
              style={{ background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', borderRadius: '8px', padding: '7px 14px', fontSize: '13px', cursor: 'pointer', marginTop: '10px' }}
            >
              Save anyway
            </button>
          </div>
        )}
      </div>

      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
            <CalendarDays size={18} /> Upcoming events
          </h3>
          <button type="button" onClick={() => { setLoadState('loading'); fetchEvents(); }} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
        {actionError ? (
          <div data-testid="calendar-action-error" style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '12px' }}>{actionError}</div>
        ) : null}
        <LoadStateRow
          loadState={loadState}
          isConnected={connected}
          loadingText="Loading your upcoming events…"
          offlineText="Waiting for the engine — your upcoming events will appear when it reconnects."
          errorText={listError || 'Your upcoming events could not be loaded.'}
          testId="calendar-events-load-state"
        />
        {loadState === 'ready' && events.length === 0 && (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No upcoming events. Mention one in chat — “I have a meeting tomorrow at 3pm” — and it will appear here.
          </div>
        )}
        {loadState === 'ready' && events.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {events.map((ev) => (
              <div key={ev.id} data-testid="calendar-event-row" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px', background: 'var(--bg-base)', borderRadius: '10px', padding: '12px 16px' }}>
                <div style={{ fontSize: '14px' }}>{ev.title}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexShrink: 0 }}>
                  <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>{formatWhen(ev.start)}</div>
                  <button
                    type="button"
                    data-testid={`calendar-cancel-${ev.id}`}
                    onClick={() => cancelEvent(ev)}
                    style={{ background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', borderRadius: '8px', padding: '5px 12px', fontSize: '12px', cursor: 'pointer' }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
