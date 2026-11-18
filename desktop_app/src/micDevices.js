import { useEffect, useState } from 'react';

// Microphone device plumbing, shared by every capture path:
// the Settings → Voice input picker (which lists the devices),
// voice mode (VoiceSession) and dictation (the chat window) —
// all three must honour the ONE selected microphone, stored in
// the engine config as `speech_input_device_id` ('' = the
// system default). Device ids are minted by the browser and
// are opaque; nothing here interprets them beyond equality.

// The getUserMedia audio constraint for a selected device id:
// `true` (any microphone — the historical behaviour) for the
// system default, an exact-device constraint otherwise.
export function audioConstraintForDevice(deviceId) {
  if (!deviceId) return true;
  return { deviceId: { exact: deviceId } };
}

// The microphones the browser can see right now, as
// {deviceId, label}. Labels are only exposed after microphone
// permission has been granted once; before that (or for a
// device the browser won't name) the row falls back to a
// positional "Microphone N" so the picker never shows a blank.
export async function listMicrophones() {
  try {
    if (!navigator.mediaDevices
        || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
      return [];
    }
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices
      .filter((d) => d && d.kind === 'audioinput')
      .map((d, i) => ({
        deviceId: d.deviceId,
        label: d.label || `Microphone ${i + 1}`,
      }));
  } catch {
    return [];
  }
}

// React hook: the current microphone list, refreshed whenever
// the browser reports a device change (plug / unplug).
export function useMicrophoneDevices() {
  const [devices, setDevices] = useState([]);
  useEffect(() => {
    let live = true;
    const update = () => {
      listMicrophones().then((found) => { if (live) setDevices(found); });
    };
    update();
    const md = navigator.mediaDevices;
    if (md && typeof md.addEventListener === 'function') {
      md.addEventListener('devicechange', update);
      return () => {
        live = false;
        md.removeEventListener('devicechange', update);
      };
    }
    return () => { live = false; };
  }, []);
  return devices;
}
