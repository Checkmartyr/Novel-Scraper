from abc import ABC, abstractmethod
from typing import Optional, Any

class BasePlatformHandler(ABC):
    """Abstract base class for platform-specific novel handlers."""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Check if URL targets this platform."""
        pass

    def is_read_only_api_request(self, method: str, url: str, headers: dict, body: Any) -> bool:
        """Declare whether this handler knows an observed POST is read-only.

        Unknown routes and methods remain disallowed. Implementations should
        match exact routes and narrowly validate the request shape.
        """
        return False

    @abstractmethod
    async def handle(self, url: str, obscura_client: Optional[Any] = None) -> Optional[str]:
        """Fetch and return synthetic HTML or None if passed through to browser."""
        pass
