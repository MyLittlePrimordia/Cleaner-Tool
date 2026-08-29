#!/usr/bin/env python3
"""Shim so `python main.py` still works — real entry is app/__main__.py"""
from app.__main__ import main
if __name__ == "__main__":
    main()
