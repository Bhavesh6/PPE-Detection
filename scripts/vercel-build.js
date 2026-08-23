/*
 * Vercel build step: tell the static frontend where the API lives.
 *
 * The frontend and the backend are deployed to different hosts, so the
 * pages cannot assume same-origin. config.js already resolves an API base
 * in a sensible order; all this does is fill in the PRODUCTION_API constant
 * from the API_BASE_URL environment variable set in the Vercel project.
 *
 * Done here rather than committed into config.js so the Space URL is not
 * baked into the repository: a rename, a fork, or a second environment
 * changes one project setting instead of a source file. Vercel gives every
 * deployment its own env, so a preview can point at a staging Space while
 * production points at the real one.
 *
 * Writes to the checkout in the build container, which is thrown away
 * afterwards - the repository is never modified.
 */

const fs = require("fs");
const path = require("path");

const CONFIG = path.join(__dirname, "..", "frontend", "js", "config.js");
const NEEDLE = "const PRODUCTION_API = '';";

const raw = (process.env.API_BASE_URL || "").trim().replace(/\/+$/, "");

if (!raw) {
  // Not fatal: a first deploy legitimately happens before the Space exists,
  // and the frontend still loads - it just falls back to same-origin and
  // every API call 404s against Vercel. Loud, because that failure looks
  // like a broken backend rather than an unset variable.
  console.warn(
    "\n[SafetyFirst] API_BASE_URL is not set.\n" +
      "  The site will build, but every API call will fail: the pages will\n" +
      "  look for the API on this Vercel domain, which does not serve one.\n" +
      "  Set it in Vercel -> Settings -> Environment Variables to the\n" +
      "  backend's public URL, e.g. https://<user>-<space>.hf.space\n"
  );
  process.exit(0);
}

if (!/^https:\/\//.test(raw)) {
  // A browser on an https:// page refuses to call an http:// endpoint, and
  // the error it gives blames mixed content rather than this setting.
  console.error(
    "\n[SafetyFirst] API_BASE_URL must start with https:// (got: " + raw + ")\n" +
      "  Vercel serves this site over HTTPS, and browsers block an HTTPS\n" +
      "  page from calling a plain HTTP API.\n"
  );
  process.exit(1);
}

const source = fs.readFileSync(CONFIG, "utf8");
if (!source.includes(NEEDLE)) {
  console.error(
    "\n[SafetyFirst] Could not find the PRODUCTION_API line in\n  " + CONFIG +
      "\n  Someone edited it. Expected exactly: " + NEEDLE + "\n"
  );
  process.exit(1);
}

fs.writeFileSync(
  CONFIG,
  source.replace(NEEDLE, "const PRODUCTION_API = '" + raw + "';"),
  "utf8"
);

console.log("[SafetyFirst] Frontend will call the API at " + raw);
