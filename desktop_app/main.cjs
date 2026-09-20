const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');

const isDev = process.env.NODE_ENV === 'development';

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    titleBarStyle: 'hidden',
    transparent: true,
    vibrancy: 'ultra-dark', // macOS
    backgroundMaterial: 'acrylic', // Windows 11
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });

  if (isDev) {
    win.loadURL('http://localhost:5173');
    // win.webContents.openDevTools();
  } else {
    win.loadFile(path.join(__dirname, 'dist/index.html'));
  }
}

app.whenReady().then(() => {
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// IPC handlers for UI to communicate with Python Engine
ipcMain.handle('close-window', (event) => {
  const webContents = event.sender
  const win = BrowserWindow.fromWebContents(webContents)
  win.close()
});

ipcMain.handle('minimize-window', (event) => {
  const webContents = event.sender
  const win = BrowserWindow.fromWebContents(webContents)
  win.minimize()
});

ipcMain.handle('maximize-window', (event) => {
  const webContents = event.sender
  const win = BrowserWindow.fromWebContents(webContents)
  if (win.isMaximized()) {
    win.unmaximize()
  } else {
    win.maximize()
  }
});

ipcMain.handle('get-auth-token', () => {
  try {
    const tokenPath = path.join(__dirname, '..', '.run', 'engine.token');
    return fs.readFileSync(tokenPath, 'utf8').trim();
  } catch (err) {
    console.error('Failed to read token:', err);
    return null;
  }
});
