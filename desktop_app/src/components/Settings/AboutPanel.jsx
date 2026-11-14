import React, { useState } from 'react';
// E6-F14: the About section's version is read from the desktop
// package manifest — one of the three mirrors of the engine's
// APP_VERSION (aether_engine/version.py), pinned equal to it by the
// epic-10 hardening tests. Never a hardcoded copy in this file.
import appPackage from '../../../package.json';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 7): the About tab, extracted from
// SettingsPanel.jsx. The workspace list still arrives through
// the shell (its WORKSPACE_LIST fetch and handler); the guide
// open attempt's status is this panel's own state.
export default function AboutPanel({ workspaces, workspacesAnswered, isConnected }) {
  const [guideStatus, setGuideStatus] = useState('');
  // Update check (15 Nov 2026): the shell checks GitHub Releases
  // (electron/updateCheck.cjs). Updating = running the new
  // installer, which replaces the whole app and every bundled
  // library with that release's pinned set — never an in-place
  // patch of individual packages.
  const [updateCheck, setUpdateCheck] = useState({ phase: 'idle', result: null });
  const [downloadStatus, setDownloadStatus] = useState('');

  const runUpdateCheck = async () => {
    if (!window.electronAPI?.checkForUpdates) {
      setUpdateCheck({
        phase: 'done',
        result: { status: 'error', message: 'Update checks are not available in this shell.' },
      });
      return;
    }
    setUpdateCheck({ phase: 'checking', result: null });
    setDownloadStatus('');
    try {
      const result = await window.electronAPI.checkForUpdates();
      setUpdateCheck({ phase: 'done', result });
    } catch (err) {
      setUpdateCheck({
        phase: 'done',
        result: { status: 'error', message: `Could not check for updates: ${err?.message || 'unknown error'}` },
      });
    }
  };

  const openDownload = async () => {
    const url = updateCheck.result?.releaseUrl;
    if (!url || !window.electronAPI?.openReleasePage) {
      setDownloadStatus('Opening the download page is not available in this shell.');
      return;
    }
    const err = await window.electronAPI.openReleasePage(url);
    setDownloadStatus(err || '');
  };

  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>About Aether-OS</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        A local-first desktop agent: a Python engine with a persistent memory, driven from this shell.
      </p>

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '24px', marginBottom: '20px' }}>
        {/* E6-F14: the version comes from the package manifest
            (the APP_VERSION mirror), never a literal here. */}
        <div style={{ fontSize: '15px', fontWeight: 500, marginBottom: '4px' }}>Aether-OS</div>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Version {appPackage.version}</div>
      </div>

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '24px', marginBottom: '20px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Updates</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
          Aether-OS updates as a whole: a new version is a new installer that replaces the entire app —
          shell, engine and every bundled library — with that release's exact pinned set. Your data
          (in ~/.aether) is kept. Nothing is downloaded or changed until you run an installer yourself.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <button
            onClick={runUpdateCheck}
            disabled={updateCheck.phase === 'checking'}
            style={{ background: 'rgba(var(--accent-rgb),0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            {updateCheck.phase === 'checking' ? 'Checking…' : 'Check for updates'}
          </button>
          {updateCheck.phase === 'done' && updateCheck.result && (
            <span
              data-testid="about-update-status"
              style={{ fontSize: '13px', color: updateCheck.result.status === 'error' ? 'var(--danger)' : 'var(--text-secondary)' }}
            >
              {updateCheck.result.message}
            </span>
          )}
          {updateCheck.result?.status === 'update-available' && (
            <button
              onClick={openDownload}
              style={{ background: 'var(--accent)', color: 'var(--bg-base)', border: 'none', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 600 }}
            >
              Download the new installer
            </button>
          )}
        </div>
        {downloadStatus && <div style={{ fontSize: '13px', color: 'var(--danger)', marginTop: '12px' }}>{downloadStatus}</div>}
      </div>

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Local documentation</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
          The system guide ships with the project (docs/AETHER_SYSTEM_GUIDE.md) and opens with your
          default Markdown reader — nothing is fetched from the network.
        </p>
        {!workspacesAnswered ? (
          <LoadStateRow
            loadState="loading"
            isConnected={isConnected}
            testId="about-workspaces-load-state"
            variant="plain"
            loadingText="Loading your workspaces…"
            offlineText="Waiting for the engine — your workspaces will appear here once it connects."
          />
        ) : workspaces.length === 0 ? (
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            Add a workspace that contains the Aether-OS project and the guide can be opened from here.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {workspaces.map((root) => (
              <div key={root.path} style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                <button
                  onClick={async () => {
                    // The shell's open-path seam (as the canvas's
                    // "Open externally" uses): a failure comes
                    // back as the shell's error string and is
                    // shown, never swallowed.
                    if (!window.electronAPI?.openPath) {
                      setGuideStatus('Opening local files is not available in this shell.');
                      return;
                    }
                    const err = await window.electronAPI.openPath(`${root.path}/docs/AETHER_SYSTEM_GUIDE.md`);
                    setGuideStatus(err ? `Could not open the guide: ${err}` : '');
                  }}
                  style={{ background: 'rgba(var(--accent-rgb),0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Open the system guide
                </button>
                <span style={{ fontSize: '12px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>{root.path}</span>
              </div>
            ))}
          </div>
        )}
        {guideStatus && <div style={{ fontSize: '13px', color: 'var(--danger)', marginTop: '12px' }}>{guideStatus}</div>}
      </div>
    </div>
  );
}
