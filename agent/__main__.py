"""Entry point: python -m agent  (or the compiled .exe)"""
import os
import sys

# Add repo root to path when running as script (not needed when packaged)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.tray import main

if __name__ == "__main__":
    backend = os.environ.get("DA_BACKEND", "http://localhost:8000")
    main(backend)
