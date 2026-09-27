from abc import ABC, abstractmethod
from typing import Optional, Any

class BasePlatformHandler(ABC):
    """Abstract base class for platform-specific novel handlers."""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Check if URL targets this platform."""
        pass

    @abstractmethod
    async def handle(self, url: str, obscura_client: Optional[Any] = None) -> Optional[str]:
        """Fetch and return synthetic HTML or None if passed through to browser."""
        pass
