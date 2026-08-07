"""Static dev server that never lets the browser cache.

`python -m http.server` sends Last-Modified and no Cache-Control, so browsers
apply heuristic caching and happily serve a stale page — you edit the CSS,
reload, and see the old design. That failure is silent and easy to mistake
for "my change didn't work".

Usage (from the frontend/ directory):
    python serve.py            # port 8000
    python serve.py 8080
"""

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Keep the console readable; errors still surface via log_error.
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = partial(NoCacheHandler, directory=".")
    with ThreadingHTTPServer(("0.0.0.0", port), handler) as httpd:
        print(f"Serving frontend on http://localhost:{port} (caching disabled)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
