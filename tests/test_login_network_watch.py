"""Unit tests for login/logout network heuristics."""

from __future__ import annotations

from cursor_spend_tray.login_network_watch import (
    cookies_include_session,
    headers_carry_session_cookie,
    response_suggests_auth_rate_limit,
    response_suggests_authenticated_session,
    response_suggests_logout,
    url_looks_like_dashboard_page,
    url_looks_like_usage_api,
)
from cursor_spend_tray.session_cookie import CURSOR_SESSION_COOKIE


def test_usage_api_alone_does_not_count_as_login() -> None:
    url = "https://cursor.com/api/dashboard/get-user-organizations"
    assert url_looks_like_usage_api(url)
    assert not response_suggests_authenticated_session(url, 200, saw_auth=True)
    assert not response_suggests_authenticated_session(url, 302, saw_auth=False)


def test_dashboard_page_alone_does_not_count_as_login() -> None:
    url = "https://cursor.com/dashboard/spending"
    assert url_looks_like_dashboard_page(url)
    assert not response_suggests_authenticated_session(url, 200, saw_auth=True)


def test_session_cookie_header_counts_as_login() -> None:
    headers = {"Cookie": f"{CURSOR_SESSION_COOKIE}=abc; other=1"}
    assert headers_carry_session_cookie(headers)
    assert response_suggests_authenticated_session(
        "https://cursor.com/dashboard/spending",
        200,
        headers=headers,
    )


def test_token_name_inside_other_cookie_value_is_ignored() -> None:
    headers = {
        "Cookie": f"__recent_auth=prefix-{CURSOR_SESSION_COOKIE}-suffix; other=1",
    }
    assert not headers_carry_session_cookie(headers)
    assert not response_suggests_authenticated_session(headers=headers)


def test_set_cookie_header_counts_as_login() -> None:
    headers = [
        {"name": "Set-Cookie", "value": f"{CURSOR_SESSION_COOKIE}=xyz; Path=/; HttpOnly"},
    ]
    assert headers_carry_session_cookie(headers)
    assert response_suggests_authenticated_session(headers=headers)


def test_cookies_include_session_requires_cursor_com_domain() -> None:
    assert cookies_include_session(
        [{"name": CURSOR_SESSION_COOKIE, "value": "1", "domain": ".cursor.com"}]
    )
    assert not cookies_include_session(
        [
            {
                "name": CURSOR_SESSION_COOKIE,
                "value": "1",
                "domain": "authenticator.cursor.sh",
            }
        ]
    )
    assert not cookies_include_session([{"name": "other", "value": "1"}])
    assert not cookies_include_session(None)


def test_logout_still_detects_auth_document_and_api_errors() -> None:
    assert response_suggests_logout(
        "https://authenticator.cursor.sh/?client_id=x",
        200,
        resource_type="Document",
    )
    assert response_suggests_logout(
        "https://cursor.com/api/dashboard/get-user-organizations",
        401,
    )
    assert not response_suggests_logout(
        "https://cursor.com/api/dashboard/get-user-organizations",
        200,
    )


def test_auth_rate_limit_detects_authenticator_429() -> None:
    assert response_suggests_auth_rate_limit(
        "https://authenticator.cursor.sh/sign-up?_rsc=abc",
        429,
    )
    assert response_suggests_auth_rate_limit(
        "https://cursor.com/api/auth/callback?code=x",
        429,
    )
    assert not response_suggests_auth_rate_limit(
        "https://authenticator.cursor.sh/sign-up",
        200,
    )
    assert not response_suggests_auth_rate_limit(
        "https://example.com/sign-up",
        429,
    )
