"""ramfix - cross-platform memory housekeeping from the command line.

The package intentionally keeps the platform-specific code isolated in
``cleaner.py`` so the CLI and reporting layers stay simple and testable.
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
