"""Entrypoint for ``python -m skillbench``.

Kept intentionally tiny so the Makefile/Dockerfile can invoke ``python -m
skillbench`` without needing the package to be pip-installed — as long as
``src/`` is on ``PYTHONPATH`` (the Docker runtime sets this), the package
resolves and we dispatch to ``main()``.
"""

from . import main

if __name__ == "__main__":
    main()
