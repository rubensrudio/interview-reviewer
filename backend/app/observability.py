"""Public observability API: structured events and timing metrics (DA-11).

Application code imports ``log_event`` and ``timed`` from here. Both write through
the redacting JSON logging configured in ``app.logging_setup``.
"""

from app.logging_setup import log_event, timed

__all__ = ["log_event", "timed"]
