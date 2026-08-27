import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # 5057 avoids colliding with other local dev servers commonly left running on 5000.
    app.run(debug=app.config["DEBUG"], port=int(os.environ.get("PORT", 5057)))
