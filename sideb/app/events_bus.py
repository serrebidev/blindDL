"""A minimal pub/sub event bus. Workers emit PipelineEvent instances; any
number of observers (rich progress rendering, logging, JSON writers) can
subscribe without the pipeline needing to know about them."""

from __future__ import annotations

import threading
from typing import Callable

from sideb.models.events import PipelineEvent

Listener = Callable[[PipelineEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners[:] = [fn for fn in self._listeners if fn is not listener]

    def emit(self, event: PipelineEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener(event)
