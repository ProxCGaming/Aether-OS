// Set the webview src from the query param the probe main passes,
// so the fixture path is not baked in. This mirrors what React does
// in ProjectPreview: set the src attribute of a <webview> with
// partition="aether-preview" to the artifact's file:// URL.
var target = new URLSearchParams(location.search).get('target');
document.getElementById('preview').setAttribute('src', target);
window.__hostTarget = target;
