"""Entry point: launch the FastAPI app with uvicorn.

Host/port are configurable via environment variables so it won't clash with any
other service already running on the Pi:

    PORT=8001 python run.py
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
