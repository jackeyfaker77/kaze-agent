"""Offline notebook experiment. Not registered with Kaze's runtime or plugins."""

from .store import Notebook, NotebookError

__all__ = ["Notebook", "NotebookError"]
