try:
    from .app import app
except ImportError:
    from app import app

# Run from repo root:
#   uvicorn backend.main:app --host 0.0.0.0 --port 8000
# Or from backend/:
#   uvicorn main:app --host 0.0.0.0 --port 8000
