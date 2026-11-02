import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from ..runtime.scheduling import CapacityExceeded, SchedulerClosed

logger = logging.getLogger(__name__)


@contextmanager
def request_errors() -> Iterator[None]:
    try:
        yield
    except CapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "scheduler_capacity", "message": str(exc)},
            headers={"Retry-After": "1"},
        ) from exc
    except SchedulerClosed as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "scheduler_closed", "message": str(exc)},
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown conversation or task") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Conversation storage is unavailable")
        raise HTTPException(
            status_code=503,
            detail="Conversation storage is unavailable",
        ) from exc
