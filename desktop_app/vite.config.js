import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// S3-M2: index.html carries the PRODUCTION Content-Security-Policy
// as a <meta> tag (see electron/securityPolicy.cjs). That policy
// forbids the inline/eval scripts Vite dev depends on (the React
// refresh preamble, eval-based sourcemaps), so in dev the tag is
// stripped here and the Electron main process serves the relaxed
// dev-only policy as a response header instead. Builds keep the
// tag untouched — ctx.server exists only for `vite dev`.
function stripProductionCspMetaInDev() {
  return {
    name: 'aether-strip-production-csp-meta-in-dev',
    transformIndexHtml(html, ctx) {
      if (!ctx.server) return html
      return html.replace(/<meta\s+http-equiv="Content-Security-Policy"[^>]*>\s*/, '')
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [react(), stripProductionCspMetaInDev()],
})
