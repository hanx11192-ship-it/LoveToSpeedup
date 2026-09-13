import os

from app import app, start_services

if __name__ == "__main__":
    start_services()
    app.run(host="0.0.0.0", port=int(os.environ.get("GATEWAY_PORT", "8000")), debug=False)
