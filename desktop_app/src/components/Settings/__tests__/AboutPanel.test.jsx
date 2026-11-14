/** Wave 5 (Annex C row 7): AboutPanel extracted from
 * SettingsPanel.jsx. The workspace list arrives as a prop (the
 * shell's WORKSPACE_LIST fetch/handler owns it); the version
 * comes from the package manifest (E6-F14) and the guide-open
 * status is the panel's own state. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import AboutPanel from '../AboutPanel';

describe('AboutPanel', () => {
  test('the version renders from the package manifest, never a literal (E6-F14)', () => {
    render(<AboutPanel workspaces={[]} workspacesAnswered isConnected />);
    expect(screen.getByText('Aether-OS')).toBeInTheDocument();
    expect(screen.getByText(/^Version \d+\.\d+\.\d+/)).toBeInTheDocument();
  });

  test('before the workspace answer the guide area shows its load state', () => {
    render(<AboutPanel workspaces={[]} workspacesAnswered={false} isConnected />);
    expect(screen.getByTestId('about-workspaces-load-state')).toBeInTheDocument();
  });

  test('answered with no workspaces, the guide area says how to enable it', () => {
    render(<AboutPanel workspaces={[]} workspacesAnswered isConnected />);
    expect(screen.getByText(/Add a workspace that contains the Aether-OS project/)).toBeInTheDocument();
  });

  test('a workspace renders the guide button; without the shell bridge the click explains itself', async () => {
    render(<AboutPanel workspaces={[{ path: '/home/user/Aether-OS' }]} workspacesAnswered isConnected />);
    expect(screen.getByText('/home/user/Aether-OS')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Open the system guide'));
    expect(await screen.findByText('Opening local files is not available in this shell.')).toBeInTheDocument();
  });

  describe('update check (15 Nov 2026)', () => {
    afterEach(() => {
      delete window.electronAPI;
    });

    test('without the shell bridge the check explains itself', async () => {
      render(<AboutPanel workspaces={[]} workspacesAnswered isConnected />);
      fireEvent.click(screen.getByText('Check for updates'));
      expect(await screen.findByText('Update checks are not available in this shell.')).toBeInTheDocument();
    });

    test('an available update is reported and the download button opens the release page', async () => {
      const openReleasePage = jest.fn(async () => '');
      window.electronAPI = {
        checkForUpdates: async () => ({
          status: 'update-available',
          currentVersion: '0.3.5',
          latestVersion: '0.4.0',
          releaseUrl: 'https://github.com/ProxCGaming/Aether-OS/releases/latest',
          message: 'Version 0.4.0 is available — you have 0.3.5.',
        }),
        openReleasePage,
      };
      render(<AboutPanel workspaces={[]} workspacesAnswered isConnected />);
      fireEvent.click(screen.getByText('Check for updates'));
      expect(await screen.findByText('Version 0.4.0 is available — you have 0.3.5.')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Download the new installer'));
      expect(openReleasePage).toHaveBeenCalledWith('https://github.com/ProxCGaming/Aether-OS/releases/latest');
    });

    test('up-to-date is reported plainly, with no download button', async () => {
      window.electronAPI = {
        checkForUpdates: async () => ({
          status: 'up-to-date',
          currentVersion: '0.3.5',
          latestVersion: '0.3.5',
          releaseUrl: 'https://github.com/ProxCGaming/Aether-OS/releases/latest',
          message: "You're on the latest version (0.3.5).",
        }),
        openReleasePage: async () => '',
      };
      render(<AboutPanel workspaces={[]} workspacesAnswered isConnected />);
      fireEvent.click(screen.getByText('Check for updates'));
      expect(await screen.findByText("You're on the latest version (0.3.5).")).toBeInTheDocument();
      expect(screen.queryByText('Download the new installer')).not.toBeInTheDocument();
    });
  });
});
