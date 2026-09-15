"""Pytest configuration for Plane MCP Server tests."""

import pytest
from plane.api.base_resource import BaseResource

from plane_mcp.moneta import apiretry


@pytest.fixture(autouse=True)
def restore_base_resource():
    """Undo ``apiretry.install()``, which patches a third-party class process-wide.

    Lives here rather than in ``test_apiretry.py`` because that module is not the only
    one that arms the patch: any test driving the real ``build_plane_client`` on the
    Cognito path calls ``install()`` too, and the wrap would then outlive the module
    that caused it and leak into every test collected afterwards.
    """
    original = {name: getattr(BaseResource, name) for name in apiretry._VERB_METHODS}
    yield
    for name, method in original.items():
        setattr(BaseResource, name, method)
