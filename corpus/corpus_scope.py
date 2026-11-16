"""Scoped permission for the append-only upload service, never general editing."""

from contextlib import contextmanager
from contextvars import ContextVar

_append_study = ContextVar("corpus_append_study", default=None)


def appending_to(study_id):
    return _append_study.get() == str(study_id)


@contextmanager
def append_scope(study_id):
    token = _append_study.set(str(study_id))
    try:
        yield
    finally:
        _append_study.reset(token)
