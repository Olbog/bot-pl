"""Точка входа: python -m bot.main"""
from .app import App, main  # noqa: F401 — App импортируют тесты

if __name__ == "__main__":
    main()
