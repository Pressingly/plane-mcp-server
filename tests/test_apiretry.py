"""Moneta fork — re-mint and retry once when Plane rejects a minted X-Api-Key.

Covers the regression Plane PR #48 exposes: with the API key authenticated before
the session, a stale cached key stops falling through to the session and instead
hard-fails every tool call for that user until the 30-day TTL expires.

The case worth reading first is ``test_only_a_named_token_rejection_is_retried``. A
permission denial reaches the SDK as the same 403 as a rejected token, so the gate is
a strict allow-list on the response ``detail`` (with the 403 status as a precondition),
not the status alone and not an exclusion of known denials. Getting that backwards
revokes a live token: re-minting calls ``_revoke_existing()``.
"""

import pytest
from plane.api.base_resource import BaseResource
from plane.errors.errors import HttpError

import plane_mcp.moneta.apiretry as apiretry
import plane_mcp.moneta.apitoken as apt

# Bound before the autouse guard below can replace the module attribute, so the
# refresh_api_key tests exercise the real implementation.
REAL_REFRESH_API_KEY = apt.refresh_api_key

TOKEN_REJECTED = {"detail": "Given API token is not valid"}
# Plane's /api/v1 permission denials are hand-rolled Response({"error": ...}, 403),
# not DRF's {"detail": ...}. Verified across all eight HTTP_403_FORBIDDEN sites in
# apps/api/plane/api/views/ (issue.py, cycle.py, module.py, intake.py).
PERMISSION_DENIED = {"error": "Only admin or creator can delete the work item"}
DRF_PERMISSION_DENIED = {"detail": "You do not have permission to perform this action."}


class _Config:
    """Stand-in for plane-sdk's Configuration (a plain mutable object)."""

    def __init__(self, api_key, identity=None):
        self.api_key = api_key
        if identity is not None:
            setattr(self, apt.MINTED_IDENTITY_ATTR, identity)


class _Resource:
    """Minimal BaseResource stand-in that records how often the verb ran."""

    def __init__(self, config, responses):
        self.config = config
        self._responses = list(responses)
        self.calls = []

    def _get(self):
        self.calls.append(self.config.api_key)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_error(status, payload):
    return HttpError(f"HTTP {status}", status, payload)


@pytest.fixture
def wrapped_get():
    """Apply the production decorator to the stand-in's verb."""
    return apiretry._retry_once(_Resource._get)


@pytest.fixture(autouse=True)
def _no_real_minting(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("refresh_api_key must be stubbed by the test")

    monkeypatch.setattr(apt, "refresh_api_key", _fail)


def _stub_refresh(monkeypatch, returns):
    seen = []

    def _refresh(identity, stale):
        seen.append((identity, stale))
        return returns

    monkeypatch.setattr(apt, "refresh_api_key", _refresh)
    return seen


# --- the retry fires ---------------------------------------------------------


def test_rejected_key_is_refreshed_and_the_request_retried_once(monkeypatch, wrapped_get):
    seen = _stub_refresh(monkeypatch, "fresh-key")
    resource = _Resource(_Config("stale-key", identity="user@acme.com"), [_http_error(403, TOKEN_REJECTED), "ok"])

    assert wrapped_get(resource) == "ok"
    assert resource.calls == ["stale-key", "fresh-key"], "retry must carry the new key"
    assert seen == [("user@acme.com", "stale-key")]
    assert resource.config.api_key == "fresh-key"


def test_retry_happens_at_most_once(monkeypatch, wrapped_get):
    _stub_refresh(monkeypatch, "fresh-key")
    resource = _Resource(
        _Config("stale-key", identity="user@acme.com"),
        [_http_error(403, TOKEN_REJECTED), _http_error(403, TOKEN_REJECTED)],
    )

    with pytest.raises(HttpError):
        wrapped_get(resource)
    assert len(resource.calls) == 2, "a second failure must surface, not loop"


# --- the guards --------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        PERMISSION_DENIED,
        DRF_PERMISSION_DENIED,
        {},
        {"detail": None},
        {"error": "something else entirely"},
        # DRF's own token-flavoured 403s, which a loosened matcher would wrongly accept.
        {"detail": "Invalid token header. No credentials provided."},
        {"detail": "Invalid token."},
        "<html>504 Gateway Timeout</html>",
        None,
    ],
    ids=[
        "plane-error",
        "drf-detail",
        "empty",
        "null-detail",
        "other-error",
        "invalid-token-header",
        "invalid-token",
        "html",
        "no-body",
    ],
)
def test_only_a_named_token_rejection_is_retried(monkeypatch, wrapped_get, body):
    """Everything that is not positively a rejected token must be left alone.

    Re-minting calls _revoke_existing(), which DELETEs every moneta-mcp token for the
    user, so misclassifying one of these would revoke a live key and strand every
    other replica.
    """
    seen = _stub_refresh(monkeypatch, "fresh-key")
    resource = _Resource(_Config("valid-key", identity="user@acme.com"), [_http_error(403, body), "ok"])

    with pytest.raises(HttpError) as excinfo:
        wrapped_get(resource)
    assert excinfo.value.status_code == 403
    assert resource.calls == ["valid-key"], "the request must not be re-issued"
    assert seen == [], "must not attempt a mint, which would revoke a live token"


def test_real_refresh_is_never_reached_on_a_plane_permission_denial(monkeypatch, wrapped_get):
    """End to end against the real refresh_api_key, not a stub.

    The stubbed tests above cannot catch a classifier that lets a denial through,
    because a stub cannot revoke anything. This one asserts _mint is never called.
    """
    monkeypatch.setattr(apt, "refresh_api_key", REAL_REFRESH_API_KEY)
    monkeypatch.setattr(apt, "_cache_get", lambda identity: "valid-key")
    monkeypatch.setattr(apt, "_cache_delete", lambda identity: pytest.fail("must not evict a live key"))
    monkeypatch.setattr(apt, "_mint", lambda: pytest.fail("must not revoke a live token on a permission denial"))

    resource = _Resource(_Config("valid-key", identity="user@acme.com"), [_http_error(403, PERMISSION_DENIED), "ok"])
    with pytest.raises(HttpError):
        wrapped_get(resource)
    assert resource.calls == ["valid-key"]


def test_unminted_key_is_left_alone(monkeypatch, wrapped_get):
    """PAT / Plane-OAuth keys are not ours to refresh."""
    _stub_refresh(monkeypatch, "fresh-key")
    resource = _Resource(_Config("pat-key"), [_http_error(403, TOKEN_REJECTED), "ok"])  # no identity tag

    with pytest.raises(HttpError):
        wrapped_get(resource)
    assert resource.calls == ["pat-key"]


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
def test_non_403_statuses_pass_through(monkeypatch, wrapped_get, status):
    """Carries the token-rejection body, so only the status check can stop the retry."""
    seen = _stub_refresh(monkeypatch, "fresh-key")
    resource = _Resource(_Config("stale-key", identity="user@acme.com"), [_http_error(status, TOKEN_REJECTED), "ok"])

    with pytest.raises(HttpError) as excinfo:
        wrapped_get(resource)
    assert excinfo.value.status_code == status
    assert resource.calls == ["stale-key"], f"{status} must not trigger a re-mint"
    assert seen == []


def test_the_rejection_phrase_is_pinned():
    """The one string separating "retry" from "revoke a live token".

    Loosening it to a substring like "token" would match DRF's "Invalid token header"
    and re-mint on it, so the value is asserted rather than left to drift.
    """
    assert apiretry._TOKEN_REJECTED_DETAIL == "given api token is not valid"


def test_the_token_rejection_phrase_is_only_honoured_under_detail(wrapped_get):
    """The allow-list keys on DRF's `detail`, so a hand-rolled `error` cannot spoof it."""
    spoofed = _http_error(403, {"error": "Given API token is not valid"})
    assert apiretry._is_token_rejection(spoofed) is False


def test_failed_mint_surfaces_the_original_error(monkeypatch, wrapped_get):
    _stub_refresh(monkeypatch, None)
    resource = _Resource(_Config("stale-key", identity="user@acme.com"), [_http_error(403, TOKEN_REJECTED), "ok"])

    with pytest.raises(HttpError):
        wrapped_get(resource)
    assert resource.calls == ["stale-key"]


def test_success_path_never_consults_the_cache(wrapped_get):
    """The autouse fixture makes any refresh_api_key call an error."""
    resource = _Resource(_Config("good-key", identity="user@acme.com"), ["ok"])
    assert wrapped_get(resource) == "ok"
    assert resource.calls == ["good-key"]


# --- install() ---------------------------------------------------------------


def test_install_wraps_every_verb_and_is_idempotent():
    # Asserted as a literal: iterating apiretry._VERB_METHODS would let the expectation
    # shrink in lockstep with production, so dropping _post/_patch/_delete would leave
    # every mutating tool unwrapped with the suite still green.
    verbs = ("_get", "_post", "_put", "_patch", "_delete")
    assert apiretry._VERB_METHODS == verbs

    apiretry.install()
    first = {name: getattr(BaseResource, name) for name in verbs}
    for name, method in first.items():
        assert getattr(method, apiretry._WRAPPED_MARKER, False), f"{name} not wrapped"

    apiretry.install()
    for name, method in first.items():
        assert getattr(BaseResource, name) is method, f"{name} was double-wrapped"


def test_install_never_raises(monkeypatch):
    """The except branch must actually run, not be skipped by an already-wrapped verb."""
    attempted = []

    def _boom(method):
        attempted.append(method)
        raise RuntimeError("boom")

    monkeypatch.setattr(apiretry, "_VERB_METHODS", ("_get",))
    monkeypatch.setattr(apiretry, "_retry_once", _boom)

    apiretry.install()  # must swallow and log rather than break startup

    assert attempted, "install() skipped the wrap, so the failure path was never exercised"


# --- refresh_api_key ---------------------------------------------------------


def test_refresh_reuses_a_key_another_request_already_minted(monkeypatch):
    """Avoids a mint stampede, where replicas revoke each other's fresh tokens."""
    monkeypatch.setattr(apt, "_cache_get", lambda identity: "someone-elses-fresh-key")
    monkeypatch.setattr(apt, "_mint", lambda: pytest.fail("must not mint when the cache already moved on"))

    assert REAL_REFRESH_API_KEY("user@acme.com", "stale-key") == "someone-elses-fresh-key"


def test_refresh_mints_when_the_cache_still_holds_the_stale_key(monkeypatch):
    deleted, stored = [], []
    monkeypatch.setattr(apt, "_cache_get", lambda identity: "stale-key")
    monkeypatch.setattr(apt, "_cache_delete", lambda identity: deleted.append(identity))
    monkeypatch.setattr(apt, "_cache_set", lambda identity, token: stored.append((identity, token)))
    monkeypatch.setattr(apt, "_mint", lambda: "fresh-key")

    assert REAL_REFRESH_API_KEY("user@acme.com", "stale-key") == "fresh-key"
    assert deleted == ["user@acme.com"], "the stale entry must be evicted before minting"
    assert stored == [("user@acme.com", "fresh-key")]


def test_cache_delete_removes_the_prefixed_key_from_both_tiers(monkeypatch):
    """Exercises the Valkey path, which every other test monkeypatches away.

    If the prefix were dropped, the dead key would survive in Valkey for the rest of
    its TTL and each later request would pay a futile re-mint.
    """
    deleted = []

    class _FakeRedis:
        def delete(self, key):
            deleted.append(key)

    monkeypatch.setattr(apt, "_redis_client", lambda: _FakeRedis())
    apt._memory_cache["user@acme.com"] = "stale-key"

    apt._cache_delete("user@acme.com")

    assert deleted == [apt._CACHE_PREFIX + "user@acme.com"]
    assert "user@acme.com" not in apt._memory_cache, "the in-process copy must go too"


def test_cache_delete_falls_back_to_memory_without_redis(monkeypatch):
    monkeypatch.setattr(apt, "_redis_client", lambda: None)
    apt._memory_cache["user@acme.com"] = "stale-key"

    apt._cache_delete("user@acme.com")

    assert "user@acme.com" not in apt._memory_cache


def test_refresh_does_not_cache_a_failed_mint(monkeypatch):
    monkeypatch.setattr(apt, "_cache_get", lambda identity: None)
    monkeypatch.setattr(apt, "_cache_delete", lambda identity: None)
    monkeypatch.setattr(apt, "_cache_set", lambda identity, token: pytest.fail("must not cache None"))
    monkeypatch.setattr(apt, "_mint", lambda: None)

    assert REAL_REFRESH_API_KEY("user@acme.com", "stale-key") is None
