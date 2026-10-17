"""Job-local recovery measurements and an explicit evaluation model budget."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time


_trace = ContextVar("recovery_trace", default=None)
_budget = ContextVar("evaluation_budget", default=None)
_ceiling = ContextVar("recovery_ceiling", default=None)
_validated = ContextVar("validated_recovery_items", default=None)


def mark_validated(row, keys):
    seen = _validated.get()
    keys = set(keys)
    row["newly_validated"] = len(keys - seen) if seen is not None else len(keys)
    if seen is not None:
        seen.update(keys)


def measured_sync(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name) as row:
                result = function(*args, **kwargs)
                count = len(result) if isinstance(result, list) else 0
                row.update(outcome="observed", newly_discovered=count, reason="Deterministic evidence awaits coverage audit")
                return result
        return wrapped
    return decorate


def stage_enabled(name):
    import os
    stages = ["recipe", "deterministic", "browser", "model", "repair"]
    ceiling = _ceiling.get() or os.getenv("NOVEL_SCRAPER_RECOVERY_CEILING", "repair")
    if ceiling not in stages:
        raise ValueError("Unknown recovery ceiling")
    return stages.index(name) <= stages.index(ceiling)


@contextmanager
def recovery_ceiling(name):
    import os
    stages = ["recipe", "deterministic", "browser", "model", "repair"]
    previous = _ceiling.get() or os.getenv("NOVEL_SCRAPER_RECOVERY_CEILING", "repair")
    token = _ceiling.set(min((name, previous), key=stages.index))
    try:
        yield
    finally:
        _ceiling.reset(token)


def measured_model(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        with stage("visual_model" if kwargs.get("images") else "model") as row:
            if not stage_enabled("model"):
                raise ModelBudgetExceeded("Model selection is beyond the active recovery ceiling")
            if self.is_available:
                reserve_model_call(bool(kwargs.get("images")))
            from src.scraper.recovery import before_deadline, operation_deadline
            from src.scraper.runtime import ScraperLimits
            result = await before_deadline(function(self, *args, **kwargs),
                min(operation_deadline(), time.monotonic() + ScraperLimits.from_env().discovery_seconds))
            row.update(outcome="proposed", reason="Structured proposal awaits deterministic verification")
            return result
    return wrapped


@contextmanager
def stage(name):
    row = {"stage": name, "attempts": 1, "seconds": 0, "newly_validated": 0,
           "outcome": "rejected", "reason": "No independently verified improvement"}
    start = time.monotonic()
    try:
        yield row
    except BaseException as error:
        from src.scraper.runtime import safe_error
        row.update(outcome="failed", reason=safe_error(error))
        raise
    finally:
        row["seconds"] = round(time.monotonic() - start, 6)
        if _trace.get() is not None:
            _trace.get().append(row)


def traced_job(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        rows = []
        token = _trace.set(rows)
        verified_token = _validated.set(set())
        try:
            result = await function(*args, **kwargs)
            result.setdefault("metrics", {})["recovery_stages"] = rows
            return result
        finally:
            _trace.reset(token)
            _validated.reset(verified_token)
    return wrapped


class ModelBudgetExceeded(RuntimeError):
    pass


@contextmanager
def evaluation_budget(model_calls=30, visual_calls=6):
    counts = {"model_calls": 0, "visual_calls": 0, "max_model_calls": model_calls,
              "max_visual_calls": visual_calls}
    token = _budget.set(counts)
    try:
        yield counts
    finally:
        _budget.reset(token)


def reserve_model_call(visual=False):
    counts = _budget.get()
    if counts is None:
        return
    if counts["model_calls"] >= counts["max_model_calls"] or (
            visual and counts["visual_calls"] >= counts["max_visual_calls"]):
        raise ModelBudgetExceeded("Configured evaluation model budget exhausted; effectiveness is unmeasured")
    import os
    ledger = os.getenv("NOVEL_SCRAPER_EVALUATION_BUDGET_FILE")
    if ledger:
        import json
        from pathlib import Path
        from src.scraper.runtime import DestinationLock, atomic_json
        path = Path(ledger).resolve()
        lock = DestinationLock(path.parent / "budget-locks", path)
        lock.acquire()
        try:
            used = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"model_calls": 0, "visual_calls": 0}
            if used["model_calls"] >= 30 or (visual and used["visual_calls"] >= 6):
                raise ModelBudgetExceeded("Shared evaluation ledger exhausted; effectiveness is unmeasured")
            used["model_calls"] += 1
            used["visual_calls"] += int(visual)
            atomic_json(path, used)
        finally:
            lock.release()
    counts["model_calls"] += 1
    counts["visual_calls"] += int(visual)
