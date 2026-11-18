import { audioConstraintForDevice, listMicrophones } from '../micDevices';

// The microphone plumbing shared by the Settings input picker,
// voice mode and dictation: one selected device id ('' = the
// system default) becomes the getUserMedia constraint every
// capture path uses.

describe('audioConstraintForDevice', () => {
  it('is plain `true` for the system default', () => {
    expect(audioConstraintForDevice('')).toBe(true);
    expect(audioConstraintForDevice(null)).toBe(true);
    expect(audioConstraintForDevice(undefined)).toBe(true);
  });

  it('is an exact-device constraint for a selected microphone', () => {
    expect(audioConstraintForDevice('mic-42')).toEqual({
      deviceId: { exact: 'mic-42' },
    });
  });
});

describe('listMicrophones', () => {
  afterEach(() => {
    delete window.navigator.mediaDevices;
  });

  it('returns only audio inputs, with positional labels for unnamed devices', async () => {
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        enumerateDevices: jest.fn(() => Promise.resolve([
          { kind: 'audioinput', deviceId: 'a', label: 'Headset' },
          { kind: 'videoinput', deviceId: 'v', label: 'Webcam' },
          { kind: 'audioinput', deviceId: 'b', label: '' },
          { kind: 'audiooutput', deviceId: 'o', label: 'Speakers' },
        ])),
      },
    });
    await expect(listMicrophones()).resolves.toEqual([
      { deviceId: 'a', label: 'Headset' },
      { deviceId: 'b', label: 'Microphone 2' },
    ]);
  });

  it('returns an empty list when enumeration is unavailable or fails', async () => {
    await expect(listMicrophones()).resolves.toEqual([]);
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: jest.fn(() => Promise.reject(new Error('nope'))) },
    });
    await expect(listMicrophones()).resolves.toEqual([]);
  });
});
