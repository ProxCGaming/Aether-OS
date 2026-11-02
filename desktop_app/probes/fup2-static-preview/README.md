# FUP-2 static-preview probe

Live Electron probe for the FUP-2 change in
`src/components/renderers/ProjectPreview.jsx`: a file-backed static
artifact preview loads through a `<webview partition="aether-preview">`
guest at a `file://` URL built from the artifact's real path, instead
of a `srcDoc` iframe (which inherits the app window's strict S3-M2
CSP, so inline scripts in static artifacts no longer ran there).

## What it proves

The host page (`host.html`) carries the app window's production CSP
verbatim (copied from `desktop_app/index.html`). Inside it, a
`<webview partition="aether-preview">` is pointed at
`fixture/index.html` over `file://`. The probe asserts, in the guest:

- the guest loaded exactly the fixture's `file://` URL;
- the fixture's **inline script ran** (the CSP did not reach the guest);
- a **relative sibling script** (`sibling.js`) ran;
- a **relative sibling stylesheet** (`style.css`) applied;
- a **relative sibling image** (`pixel.png`) loaded;
- the guest has **no `electronAPI`** bridge (M01 isolation intact).

## Run

```sh
xvfb-run -a desktop_app/node_modules/electron/dist/electron \
  --no-sandbox desktop_app/probes/fup2-static-preview/main.cjs
```

(xvfb/`--no-sandbox` are for headless/root VMs; on a normal desktop
just run the electron binary on `main.cjs`.)

It prints one `PROBE_RESULT {...}` JSON line and exits 0 on success.

## Recorded result

Electron 44.4.3, Linux VM, 2 Nov 2026 — all checks true:

```json
{"ok":true,"checks":{"guestUrlIsFixtureFile":true,"inlineScriptRan":true,"siblingScriptRan":true,"siblingCssApplied":true,"siblingImageLoaded":true,"noBridgeInGuest":true}}
```
