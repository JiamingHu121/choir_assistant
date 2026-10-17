# Native touch selection regression

This optional developer test runs the actual workbench JavaScript and CSS in an
isolated Chrome profile against a small loopback SVG fixture. It sends native
touch input through Playwright/CDP; it does not dispatch DOM pointer events or
access a microphone, user account, production website, or personal browser data.

Use Node 20+ with Playwright available in your development environment and Chrome
installed, then run from the repository root:

```sh
node --test tests/browser/touch_selection.cjs
```

For externally installed test packages, set `NODE_PATH` to their `node_modules`
directory. `CHOIR_BROWSER_EXECUTABLE` optionally selects a Chromium executable;
`CHOIR_TOUCH_PROOF_DIR` optionally saves a fixture screenshot. Neither is a
production setting or runtime dependency of the plugin.

The cases cover tap replacement, explicit additive selection, native scroll,
box selection in both directions, cancellation, multitouch, Escape, view/model
changes, desktop modifiers, stable synthesis IDs, and busy state. Chromium touch
emulation does not establish behavior on a physical iOS Safari device.
