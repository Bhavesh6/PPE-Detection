// Dev-only. This file has no effect on the deployed site — backend/app.py
// and the Dockerfile serve frontend/*.html, css/, and js/ directly as static
// files, unbuilt, exactly as before. `npm run verify` (from this directory)
// starts a Vite dev server over the SAME unmodified files purely so Reticle
// has a live app to attach to while checking a change actually works.
//
// No React: this frontend is deliberately vanilla HTML/CSS/JS (see the
// project README's Tech stack section) and stays that way. @reticlehq/vite-plugin
// is a standalone plugin — react() is only needed for JSX source-stamping,
// which does not apply here regardless (see the js/reticle-dev.js comment
// for what that trade-off costs).
import { defineConfig } from "vite";
import { reticle } from "@reticlehq/vite-plugin";

export default defineConfig({
  root: ".",
  plugins: [reticle()],
  server: {
    // Every page here already resolves its API base itself (js/config.js);
    // nothing in this file needs to know the backend's address.
    open: "/index.html",
  },
});
