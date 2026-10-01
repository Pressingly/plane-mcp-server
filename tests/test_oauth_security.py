"""End-to-end test for the OAuth redirect attack reported by security researcher.

Covers the upstream Plane-OAuth provider. The Cognito path, including clients
stored before the allowlist was set, is covered in test_redirect_allowlist.py.

Attack scenario:
  1. Attacker registers a malicious OAuth client with redirect_uri=https://attacker.com/steal
  2. Attacker crafts an authorization URL and tricks victim into visiting it
  3. Victim approves on the legitimate-looking consent page
  4. Server redirects auth code to attacker's domain
  5. Attacker exchanges code for access token cross-origin (enabled by CORS credentials:true)
  6. Attacker has full read/write access to victim's Plane workspace

Since fastmcp 3.4 the attack stops at step 1: /register rejects any redirect_uri
outside the allowlist. CORS still guards step 5.
"""

import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.testclient import TestClient

from plane_mcp.auth import PlaneOAuthProvider


# Exact allowed patterns from plane_mcp/server.py
ALLOWED_REDIRECT_URI_PATTERNS = [
    "http://localhost:*",
    "http://localhost:*/*",
    "http://127.0.0.1:*",
    "http://127.0.0.1:*/*",
    "cursor://*",
    "vscode://*",
    "vscode-insiders://*",
    "windsurf://*",
    "claude://*",
]


@pytest.fixture(scope="module")
def app():
    """Build the real server app (same wiring as __main__.py) with dummy credentials."""
    oauth_mcp = FastMCP(
        "Plane MCP Server",
        auth=PlaneOAuthProvider(
            client_id="test-client-id",
            client_secret="test-client-secret",
            base_url="http://localhost:8211",
            plane_base_url="http://localhost:9999",
            plane_internal_base_url="http://localhost:9999",
            client_storage=MemoryStore(),
            required_scopes=["read", "write"],
            allowed_client_redirect_uris=ALLOWED_REDIRECT_URI_PATTERNS,
            require_authorization_consent=False,
        ),
    )

    oauth_app = oauth_mcp.http_app()

    starlette_app = Starlette(
        routes=[Mount("/", app=oauth_app)],
    )

    # Same CORS config as __main__.py
    starlette_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return starlette_app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app, follow_redirects=False)


class TestOAuthRedirectAttack:
    """Verify the attack is blocked at registration and at the CORS layer.

    The allowed_client_redirect_uris patterns restrict which client URIs can
    be registered and which the proxy will redirect to after the upstream
    callback completes.
    """

    def _register(self, client: TestClient, redirect_uri: str):
        return client.post(
            "/register",
            json={
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )

    def _register_client(self, client: TestClient, redirect_uri: str) -> dict:
        """Register an OAuth client with the given redirect_uri."""
        response = self._register(client, redirect_uri)
        assert response.status_code == 201, f"Registration failed: {response.text}"
        data = response.json()
        assert "client_id" in data
        return data

    def test_full_attack_is_blocked(self, client: TestClient) -> None:
        """Step 1 of the attack fails: the attacker cannot register their URI.

        Since fastmcp 3.4, DCR checks every requested redirect_uri against the
        allowlist, so no client carrying the attacker's URI ever reaches /authorize.
        """
        response = self._register(client, "https://attacker.com/steal")

        assert response.status_code == 400, f"Attacker URI was registered: {response.text}"
        assert response.json()["error"] == "invalid_redirect_uri"

    @pytest.mark.parametrize(
        "malicious_uri",
        [
            "https://attacker.com/steal",
            "http://evil.com/callback",
            "javascript:alert(document.cookie)",
            "data:text/html,<script>fetch('https://evil.com')</script>",
            "myapp://callback",
        ],
        ids=[
            "attacker-domain",
            "evil-domain",
            "javascript-injection",
            "data-uri-injection",
            "unknown-protocol",
        ],
    )
    def test_malicious_uris_are_rejected_at_registration(self, client: TestClient, malicious_uri: str) -> None:
        response = self._register(client, malicious_uri)

        assert response.status_code == 400, f"Malicious URI was registered: {response.text}"
        assert response.json()["error"] == "invalid_redirect_uri"

    def test_legitimate_redirect_uri_passes(self, client: TestClient) -> None:
        """Sanity check: legitimate localhost URI is accepted and the flow proceeds."""
        legitimate_uri = "http://localhost:3000/callback"

        reg = self._register_client(client, legitimate_uri)

        response = client.get(
            "/authorize",
            params={
                "client_id": reg["client_id"],
                "redirect_uri": legitimate_uri,
                "response_type": "code",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256",
            },
        )

        # Should proceed with the OAuth flow (302 to upstream), not error
        assert response.status_code != 400, (
            f"Legitimate redirect URI was rejected: {response.text}"
        )

    def test_cors_blocks_cross_origin_token_theft(self, client: TestClient) -> None:
        """Step 5: Even if attacker got a code, CORS blocks cross-origin token exchange.

        With allow_credentials=False, browsers will not attach cookies or allow
        attacker JS to read the response from a cross-origin POST to /token.
        The combination of Access-Control-Allow-Origin: * with credentials:true
        was the original vulnerability — now credentials is false.
        """
        response = client.options(
            "/token",
            headers={
                "Origin": "https://attacker.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        # Credentials must NOT be allowed
        assert response.headers.get("access-control-allow-credentials") != "true", (
            "VULNERABILITY: CORS allows credentials — attacker JS can steal tokens cross-origin"
        )

        # Wildcard origin is fine without credentials
        # (browsers enforce that * + credentials:true is invalid, so this combo is safe)
        assert response.headers.get("access-control-allow-origin") == "*"
