"""Plazo monotónico compartido por todo el arranque, solo en el hilo que lo usa."""
from contextlib import contextmanager
from contextvars import ContextVar
import subprocess
import threading
import time


class StartupExpired(TimeoutError):
    pass


class StartupCancelled(Exception):
    pass


_current = ContextVar("groove_startup_budget", default=None)


def current_budget():
    return _current.get()


class StartupBudget:
    def __init__(self, seconds, cancelled=None, clock=time.monotonic, also_cancelled=None):
        self.clock = clock
        self.deadline = clock() + seconds
        self.cancelled = cancelled or threading.Event()
        self.also_cancelled = also_cancelled

    def remaining(self):
        if self.cancelled.is_set() or (self.also_cancelled is not None and self.also_cancelled.is_set()):
            raise StartupCancelled("arranque cancelado")
        seconds = self.deadline - self.clock()
        if seconds <= 0:
            raise StartupExpired("agotado el plazo de arranque de Spotify")
        return seconds

    def timeout(self, maximum):
        return min(maximum, self.remaining())

    def wait(self, seconds, wait=None):
        delay = min(seconds, self.remaining())
        if (wait or self.cancelled.wait)(delay):
            raise StartupCancelled("arranque cancelado")
        self.remaining()

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)


@contextmanager
def budget_lock(lock):
    budget = current_budget()
    if budget is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=budget.timeout(0.1)):
            pass
        try:
            budget.remaining()
        except Exception:
            lock.release()
            raise
    try:
        yield
    finally:
        lock.release()


def run_budgeted(command, run, maximum, **kwargs):
    """Acota systemctl y permite cancelar su cliente, sin cancelar la unidad."""
    budget = current_budget()
    timeout = budget.timeout(maximum) if budget is not None else maximum
    if budget is None or run is not subprocess.run:
        return run(command, timeout=timeout, **kwargs)
    options = dict(kwargs)
    if options.pop("capture_output", False):
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, **options)
    command_deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                remaining = command_deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                stdout, stderr = process.communicate(timeout=min(remaining, budget.timeout(0.1)))
                budget.remaining()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                budget.remaining()
                if time.monotonic() >= command_deadline:
                    raise
    except BaseException:
        process.kill()
        process.communicate()
        raise
