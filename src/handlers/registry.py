from typing import List, Optional, Any
from src.handlers.base import BasePlatformHandler

class HandlerRegistry:
    """Registry for platform-specific novel handlers."""

    def __init__(self) -> None:
        self._handlers: List[BasePlatformHandler] = []

    def register(self, handler: BasePlatformHandler) -> None:
        """Register a platform handler."""
        self._handlers.append(handler)

    def find_handler(self, url: str) -> Optional[BasePlatformHandler]:
        """Find a handler that matches the given URL."""
        if not url:
            return None
        for handler in self._handlers:
            if handler.matches(url):
                return handler
        return None

    @property
    def handlers(self) -> List[BasePlatformHandler]:
        return list(self._handlers)

registry = HandlerRegistry()
