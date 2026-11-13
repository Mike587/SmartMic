# -*- coding: utf-8 -*-
"""Unit tests for MS_logging (standard library only)."""
import logging

import MS_logging


def test_set_logging_idempotent():
    name = "smartmic_logging_test"
    log1 = MS_logging.set_logging(name)
    n = len(log1.handlers)
    log2 = MS_logging.set_logging(name)
    assert log2 is log1
    assert n >= 1
    assert len(log2.handlers) == n   # no duplicate handlers on repeated calls


def test_set_logging_default_is_the_shared_logger():
    assert MS_logging.set_logging() is logging.getLogger(MS_logging.LOGGER_NAME)


def test_ensure_utf8_stdout_never_raises(monkeypatch):
    class Broken:
        def reconfigure(self, **kwargs):
            raise OSError("no console")

    monkeypatch.setattr(MS_logging.sys, "stdout", Broken())
    MS_logging.ensure_utf8_stdout()
