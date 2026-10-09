from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from agent.model_runtime.errors import AuthenticationError, RateLimitError

from .store import Credential, CredentialStore

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_BASE = "https://auth.openai.com"
CODEX_TOKEN_URL = f"{CODEX_AUTH_BASE}/oauth/token"
CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = "0.144.1"
_REFRESH_SKEW_SECONDS = 120


@dataclass(frozen=True)
class BrowserLogin:
    authorization_url: str = field(repr=False)
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    nonce: str = field(repr=False)
    redirect_uri: str


class CodexAuthDriver:
    """执行 Codex 浏览器 OAuth 登录并提供可刷新的请求头。"""

    def __init__(self, store: CredentialStore, credential_id: str) -> None:
        self.store = store
        self.credential_id = credential_id

    def begin_browser_login(self, redirect_uri: str) -> BrowserLogin:
        """每次授权生成独立的 state、nonce 和 PKCE，密钥只留在后端。"""
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        params = {
            "response_type": "code",
            "client_id": CODEX_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs",
        }
        return BrowserLogin(f"{CODEX_AUTH_BASE}/oauth/authorize?{urlencode(params)}", state, verifier, nonce, redirect_uri)

    def complete_browser_login(self, login: BrowserLogin, authorization_code: str) -> Credential:
        """交换本机回调收到的授权码；由调用方在确认未取消后保存。"""
        if not authorization_code:
            raise AuthenticationError("OpenAI 回调缺少授权结果，请重新登录")
        response = httpx.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": login.redirect_uri,
                "client_id": CODEX_CLIENT_ID,
                "code_verifier": login.code_verifier,
            },
            timeout=20,
        )
        if response.status_code == 429:
            raise RateLimitError("Codex 登录请求被限流，请稍后重试")
        self._require_success(response, "Codex 登录授权交换失败，请重新登录")
        data = response.json()
        claims = _id_token_claims(str(data.get("id_token") or ""))
        returned_nonce = claims.get("nonce")
        if not isinstance(returned_nonce, str) or not secrets.compare_digest(returned_nonce, login.nonce):
            raise AuthenticationError("Codex 登录身份校验失败，请重新登录")
        return self._credential_from_token(data)

    def headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        credential = self.store.get(self.credential_id)
        self._validate_credential(credential)
        if force_refresh or self._expires_soon(credential):
            credential = self.refresh(expected_access_token=credential.access_token)
        headers = {"Authorization": f"Bearer {credential.access_token}"}
        if credential.account_id:
            headers["ChatGPT-Account-ID"] = credential.account_id
        return headers

    def refresh(self, *, expected_access_token: str | None = None) -> Credential:
        """在跨进程锁内刷新并原子保存 rotation token。"""
        with self.store.locked():
            current = self.store.get(self.credential_id)
            if expected_access_token is not None and current.access_token != expected_access_token:
                return current
            if not current.refresh_token:
                raise AuthenticationError("Codex refresh token 缺失，请重新登录")
            response = httpx.post(
                CODEX_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                    "client_id": CODEX_CLIENT_ID,
                },
                timeout=20,
            )
            if response.status_code == 429:
                raise RateLimitError("Codex token 刷新被限流")
            self._require_success(response, "Codex token 刷新失败，请重新登录")
            refreshed = self._credential_from_token(
                response.json(),
                fallback_account_id=current.account_id,
                fallback_refresh_token=current.refresh_token,
            )
            self.store.replace_locked(self.credential_id, refreshed)
            return refreshed

    @staticmethod
    def _credential_from_token(
        data: dict,
        fallback_account_id: str = "",
        fallback_refresh_token: str = "",
    ) -> Credential:
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or fallback_refresh_token)
        if not access_token or not refresh_token:
            raise AuthenticationError("Codex token 响应缺少必要字段")
        id_token = str(data.get("id_token") or "")
        if id_token:
            resolved_account_id = _account_id_from_jwt(id_token)
        elif fallback_account_id:
            resolved_account_id = fallback_account_id
        else:
            raise AuthenticationError("Codex token 响应缺少 id_token")
        expires_in = int(data.get("expires_in") or 3600)
        now = datetime.now(timezone.utc)
        return Credential(
            driver="codex",
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=resolved_account_id,
            expires_at=(now + timedelta(seconds=expires_in)).isoformat(),
            updated_at=now.isoformat(),
        )

    @staticmethod
    def _expires_soon(credential: Credential) -> bool:
        if not credential.expires_at:
            return True
        expires = datetime.fromisoformat(credential.expires_at.replace("Z", "+00:00"))
        return expires <= datetime.now(timezone.utc) + timedelta(seconds=_REFRESH_SKEW_SECONDS)

    @staticmethod
    def _validate_credential(credential: Credential) -> None:
        if credential.driver != "codex":
            raise AuthenticationError("Codex auth 引用了非 Codex 凭据")
        if not credential.access_token or not credential.account_id:
            raise AuthenticationError("Codex 凭据缺少 access_token 或 account_id")

    @staticmethod
    def _require_success(response: httpx.Response, message: str) -> None:
        if response.status_code < 400:
            return
        raise AuthenticationError(f"{message} (HTTP {response.status_code})")


def _id_token_claims(token: str) -> dict:
    """只解析固定 HTTPS OAuth 端点返回的 ID token，不接受浏览器提交的 token。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, UnicodeDecodeError) as exc:
        raise AuthenticationError("Codex ID token 不是有效 JWT") from exc
    if not isinstance(claims, dict):
        raise AuthenticationError("Codex ID token 声明结构无效")
    return claims


def _account_id_from_jwt(token: str) -> str:
    """解析 OAuth 端点签发的账号路由声明。"""
    claims = _id_token_claims(token)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    if not isinstance(account_id, str) or not account_id:
        raise AuthenticationError("Codex token 缺少 chatgpt_account_id")
    return account_id
