"""Host entry point; the launcher imports only the Python standard library."""

from .harbor.launch import main

if __name__ == "__main__":
    raise SystemExit(main())
