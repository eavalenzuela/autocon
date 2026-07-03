"""Test fixtures for autocon.

Makes the repo root importable and stubs out watchdog if it isn't installed —
the unit tests only exercise autocon's pure helper functions, so the runtime
file-watching dependency isn't required to run them.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import watchdog.observers  # noqa: F401
    import watchdog.events  # noqa: F401
except ModuleNotFoundError:
    watchdog = types.ModuleType("watchdog")
    observers = types.ModuleType("watchdog.observers")
    events = types.ModuleType("watchdog.events")

    class Observer:  # minimal stand-in; never started by the unit tests
        def schedule(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def join(self):
            pass

    class FileSystemEventHandler:
        pass

    observers.Observer = Observer
    events.FileSystemEventHandler = FileSystemEventHandler
    watchdog.observers = observers
    watchdog.events = events
    sys.modules["watchdog"] = watchdog
    sys.modules["watchdog.observers"] = observers
    sys.modules["watchdog.events"] = events
