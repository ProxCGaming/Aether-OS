# Aether-OS desktop app

The Electron + React shell for Aether-OS. It is the only frontend:
the retired PySide6 UI (`aether_ui/`) was deleted on 2026-11-02.

The shell renders the chat, dashboard, projects, and settings UI and
talks to the local Python engine (`aether_engine`, one level up) over
a WebSocket on port 8000. The engine's auth token is read and held
only by the Electron main process and sent as an `Authorization`
header — the renderer never holds it (S1-M3).

## Layout

- `main.cjs` — Electron main process: window/tray lifecycle, engine
  connection and token handling, IPC handlers.
- `electron/` — main-process helpers: `engineLifecycle.cjs` spawns
  the bundled engine (or reuses one already answering on port 8000),
  plus the preload bridges and voice/RTC support.
- `src/` — the React UI (Vite). `src/contexts/SocketContext` is the
  single WebSocket facade the components subscribe through.
- `src/__tests__/` — the Jest suites.

## Develop

```sh
npm install
npm run electron:dev   # Vite on :5173 + Electron against it
npm run dev            # Vite only (browser preview of the UI)
```

## Test

```sh
npm test               # Jest (jsdom, React Testing Library)
npm run lint           # oxlint
```

## Build

```sh
npm run build          # Vite production build into dist/
npm run dist           # build + electron-builder packaging
```
