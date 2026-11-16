# -*- coding: utf-8 -*-
"""Unit tests for MS_CD7_API_LoA, the entry point that only imports names.

The code is in MS_CD7_devices, MS_definite_focus and MS_CD7_procedures. These
tests make sure that the entry point and those modules stay in step.
"""
import inspect

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms                 # noqa: E402
import MS_CD7_devices as devices            # noqa: E402
import MS_CD7_procedures as procedures      # noqa: E402
import MS_definite_focus as dfocus          # noqa: E402
import MS_limits                            # noqa: E402

OWNERS = (devices, dfocus, procedures)

# A public name of an owner module that the entry point does not export.
NOT_EXPORTED = {"Resettable"}


def _public_definitions(module):
    """The public functions and classes that ``module`` itself defines."""
    return {name for name, obj in vars(module).items()
            if not name.startswith("_") and (inspect.isfunction(obj) or inspect.isclass(obj))
            and obj.__module__ == module.__name__}


def test_all_has_no_duplicate_and_every_name_exists():
    assert len(ms.__all__) == len(set(ms.__all__))
    for name in ms.__all__:
        assert hasattr(ms, name), name


def test_each_exported_name_is_the_object_of_its_module():
    for name in ms.__all__:
        owner = next((m for m in (*OWNERS, MS_limits) if hasattr(m, name)), None)
        assert owner is not None, f"{name} is in no owner module"
        assert getattr(ms, name) is getattr(owner, name), name


def test_each_public_function_and_class_is_exported():
    defined = set().union(*(_public_definitions(m) for m in OWNERS))
    # ImmersionObjectiveActive is a class of MS_zenapi_objectivechanger with a
    # second name in MS_CD7_devices, so it is not a definition of an owner.
    assert defined - NOT_EXPORTED <= set(ms.__all__), sorted(defined - set(ms.__all__))


def test_each_exported_name_is_in_the_index_of_the_docstring():
    limits = {name for name in ms.__all__ if name.isupper()}
    missing = [name for name in ms.__all__ if name not in limits and name not in ms.__doc__]
    assert missing == []


def test_the_entry_point_defines_no_code():
    assert _public_definitions(ms) == set()
