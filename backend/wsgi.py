import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # 5057 avoids colliding with other local dev servers commonly left running on 5000.
    port = int(os.environ.get("PORT", 5057))
    # werkzeug is quietened to WARNING in configure_logging, which also hides
    # its own "Running on ..." line — so say where the app is ourselves. Only
    # in the parent process: the debug reloader re-runs this in a child.
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        print(f" * OIA Duty Roster running on http://127.0.0.1:{port}", flush=True)
    app.run(debug=app.config["DEBUG"], port=port)
