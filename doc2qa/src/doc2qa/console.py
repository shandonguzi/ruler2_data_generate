"""Console-only progress rendering; persistent logs remain append-only."""

import logging

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


class ProgressConsoleHandler(logging.Handler):
    def __init__(self, console=None):
        super().__init__()
        self.console = console if console is not None else Console(stderr=True)
        self.active = {}
        self.live = None

    def _refresh(self):
        if self.live is None:
            self.live = Live(
                console=self.console,
                auto_refresh=False,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start()
        self.live.update(Group(*(Text(line) for line in self.active.values())), refresh=True)

    def emit(self, record):
        try:
            event = getattr(record, "progress_event", {})
            operation_id = event.get("operation_id")
            status = event.get("status")
            text = self.format(record)
            if not operation_id:
                self.console.print(Text(text), soft_wrap=True)
                return
            if status == "started":
                self.active[operation_id] = text
                if self.console.is_terminal:
                    self._refresh()
                else:
                    self.console.print(Text(text), soft_wrap=True)
                return
            if status in {"completed", "failed", "interrupted"}:
                original = self.active.pop(operation_id, text)
                marker = "✅" if status == "completed" else "❌"
                elapsed = event.get("elapsed_seconds", 0)
                finished = f"{original} {marker} ({elapsed:.1f}s)"
                if event.get("error_type"):
                    finished += f" {event['error_type']}: {event.get('error_message', '')}"
                if self.live is not None:
                    self._refresh()
                    if not self.active:
                        self.live.stop()
                        self.live = None
                self.console.print(Text(finished), soft_wrap=True)
                return
            self.console.print(Text(text), soft_wrap=True)
        except Exception:
            self.handleError(record)

    def close(self):
        if self.live is not None:
            self.live.stop()
            self.live = None
        super().close()


def configure_logging():
    handler = ProgressConsoleHandler()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", handlers=[handler])
    # OpenAI SDK versions use either the original names or httpx2/httpcore2.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
