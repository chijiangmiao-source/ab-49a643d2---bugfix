"""ASGI 入口：uvicorn app.asgi:app"""

from .main import create_app

app = create_app()
