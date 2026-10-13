# JudgeUI Flask UI shell

This repository now includes a lightweight Flask-based control layer and static HTML/JS UI for local operation.

## Features

- Dashboard status page
- Pending queue listing
- Solution detail panel
- Config editor
- Logs viewer
- Start/stop controls for the internal judger process in demo mode

## Quick start

1. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Start the app:

   ```bash
   python app.py
   ```

3. Open http://localhost:5000 in a browser.

## Notes

- The app automatically tries to launch the repo's judger binary when it is available.
- If the C++ binary is not built yet, the UI stays in demo mode and exposes mock task data.
- The app serves static frontend files from the `web/` directory.

## Production note

For production, prefer running the Flask app behind Gunicorn and a reverse proxy like Nginx.
