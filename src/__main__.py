#!/usr/bin/env python3
"""Module entry point"""

try:
    from .main import main
except ImportError:
    from src.main import main

if __name__ == "__main__":
    main()
