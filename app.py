"""Bookly Support Agent — entry point. Agent logic: agent/ ; HTTP: _infra/server.py"""

from _infra.server import app

if __name__ == "__main__":
    # Avoid macOS AirPlay Receiver, which binds *:5000 and returns 403 in Chrome.
    app.run(debug=True, host="127.0.0.1", port=5050, threaded=True)
