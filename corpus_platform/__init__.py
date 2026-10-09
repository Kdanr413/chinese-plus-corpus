"""Unified operations layer for the Chinese Plus corpus platform."""

from .paths import PROJECT_ROOT_ENV, ProjectPaths, resolve_project_root
from .service import PlatformCoordinator

__all__ = [
    "PROJECT_ROOT_ENV",
    "PlatformCoordinator",
    "ProjectPaths",
    "resolve_project_root",
]
