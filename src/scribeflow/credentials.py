"""Secrets from the environment or the user's macOS Keychain, never from code."""

from __future__ import annotations

import getpass
import os
import sys

SERVICE = "ScribeFlow/OpenAI"
ACCOUNT = "api-key"


def _keychain():
    if sys.platform != "darwin":
        raise RuntimeError("키체인 저장은 macOS에서만 지원합니다. 환경변수를 사용하세요.")
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("키체인 사용을 위해 uv sync를 실행하세요.") from exc
    return keyring


def get_api_key() -> str | None:
    """Explicit environment variable takes precedence over local Keychain."""
    value = os.environ.get("OPENAI_API_KEY")
    if value:
        return value
    if sys.platform == "darwin":
        try:
            return _keychain().get_password(SERVICE, ACCOUNT)
        except Exception as exc:
            raise RuntimeError(
                "macOS 키체인을 읽지 못했습니다. 키체인 접근 권한을 확인하세요."
            ) from exc
    return None


def save_interactive() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("맥북 터미널에서 직접 실행해 API 키를 입력하세요.")
    keychain = _keychain()
    value = getpass.getpass("OpenAI API 키 (화면에 표시되지 않음): ").strip()
    if not value:
        raise ValueError("비어 있는 API 키는 저장할 수 없습니다.")
    try:
        keychain.set_password(SERVICE, ACCOUNT, value)
    except Exception as exc:
        raise RuntimeError("키체인 저장에 실패했습니다.") from exc


def remove_saved_key() -> None:
    keychain = _keychain()
    try:
        if keychain.get_password(SERVICE, ACCOUNT) is not None:
            keychain.delete_password(SERVICE, ACCOUNT)
    except Exception as exc:
        raise RuntimeError("키체인에서 키를 삭제하지 못했습니다.") from exc
