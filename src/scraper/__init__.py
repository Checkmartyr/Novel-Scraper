"""Scraper engine and storage modules."""

def __getattr__(name):
    # Runtime helpers are imported by the browser before the agent graph exists.
    # Delay convenience exports so importing them does not create a cycle.
    if name == "BatchScraperRunner":
        from src.scraper.batch_runner import BatchScraperRunner
        return BatchScraperRunner
    if name in {"NovelStorage", "sanitize_filename"}:
        from src.scraper import storage
        return getattr(storage, name)
    raise AttributeError(name)

__all__ = [
    "BatchScraperRunner",
    "NovelStorage",
    "sanitize_filename",
]
