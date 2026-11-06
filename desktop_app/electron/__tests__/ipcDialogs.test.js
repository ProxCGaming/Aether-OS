/**
 * Wave 5 (Annex C row 12): the native-dialog IPC actions,
 * extracted from main.cjs into electron/ipcDialogs.cjs. The
 * registrations, the S3-M3 sender guard and the show-open-dialog
 * options validation stay in main.cjs; this suite drives the
 * dialog calls themselves.
 */
const { createIpcDialogs } = require('../ipcDialogs.cjs');

function makeDialogs(result) {
  const win = { id: 'win' };
  const calls = [];
  const dialogs = createIpcDialogs({
    BrowserWindow: {
      fromWebContents: (sender) => {
        calls.push(['fromWebContents', sender]);
        return win;
      },
    },
    dialog: {
      showOpenDialog: async (w, options) => {
        calls.push(['showOpenDialog', w, options]);
        return result;
      },
    },
  });
  return { dialogs, calls, win };
}

const event = { sender: { id: 'sender' } };

describe('createIpcDialogs', () => {
  it('selectDirectory opens a directory picker on the sender window and returns the first path', async () => {
    const { dialogs, calls, win } = makeDialogs({ canceled: false, filePaths: ['/chosen', '/other'] });
    await expect(dialogs.selectDirectory(event)).resolves.toBe('/chosen');
    expect(calls[0]).toEqual(['fromWebContents', event.sender]);
    expect(calls[1]).toEqual(['showOpenDialog', win, {
      properties: ['openDirectory', 'createDirectory'],
      title: 'Select Download Directory',
    }]);
  });

  it('selectDirectory returns null when the picker is canceled', async () => {
    const { dialogs } = makeDialogs({ canceled: true, filePaths: [] });
    await expect(dialogs.selectDirectory(event)).resolves.toBeNull();
  });

  it('showOpenDialog passes the validated options through', async () => {
    const { dialogs, calls, win } = makeDialogs({ canceled: false, filePaths: ['/picked'] });
    const options = { properties: ['openFile'], title: 'Pick one' };
    await expect(dialogs.showOpenDialog(event, options)).resolves.toBe('/picked');
    expect(calls[1]).toEqual(['showOpenDialog', win, options]);
  });

  it('showOpenDialog falls back to the folder defaults without validated options', async () => {
    const { dialogs, calls, win } = makeDialogs({ canceled: false, filePaths: ['/dir'] });
    await expect(dialogs.showOpenDialog(event, null)).resolves.toBe('/dir');
    expect(calls[1]).toEqual(['showOpenDialog', win, {
      properties: ['openDirectory'],
      title: 'Select Folder',
    }]);
  });

  it('showOpenDialog returns null when canceled', async () => {
    const { dialogs } = makeDialogs({ canceled: true, filePaths: [] });
    await expect(dialogs.showOpenDialog(event, null)).resolves.toBeNull();
  });
});
