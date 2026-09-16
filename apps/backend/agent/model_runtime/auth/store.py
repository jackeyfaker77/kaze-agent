"""Hasaki 独立凭据存储；Windows 使用当前用户的 DPAPI 加密。"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from agent.model_runtime.errors import AuthenticationError

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class Credential:
    driver: str
    access_token: str
    refresh_token: str = ""
    account_id: str = ""
    expires_at: str = ""
    updated_at: str = ""


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """使用 Windows 用户凭据加解密，绝不退回明文。"""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AuthenticationError("Windows 凭据加解密失败，请使用原 Windows 用户重新登录")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())

    def get(self, credential_id: str) -> Credential:
        raw = self._read_document()["credentials"].get(credential_id)
        if not isinstance(raw, dict):
            raise AuthenticationError("此 Codex 连接尚未登录，请在模型连接中完成授权")
        try:
            return Credential(**raw)
        except TypeError as exc:
            raise AuthenticationError("Codex 凭据结构无效，请重新登录") from exc

    def metadata(self) -> dict[str, dict[str, str]]:
        return {key: {"driver": value["driver"]} for key, value in self._read_document()["credentials"].items()}

    def put(self, credential_id: str, credential: Credential) -> None:
        with self.locked():
            self.replace_locked(credential_id, credential)

    def replace_locked(self, credential_id: str, credential: Credential) -> None:
        data = self._read_document()
        data["credentials"][credential_id] = asdict(credential)
        self._write_document(data)

    @contextmanager
    def locked(self):
        """用线程锁和文件锁串行化凭据刷新与原子写入。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    deadline = time.monotonic() + 60
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise AuthenticationError("Codex 凭据正在更新，请稍后重试") from None
                            time.sleep(0.05)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_document(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "credentials": {}}
        data = self.path.read_bytes()
        if os.name == "nt":
            data = _protect(data, decrypt=True)
        try:
            raw = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise AuthenticationError("Codex 凭据文件损坏，请重新登录") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("credentials"), dict):
            raise AuthenticationError("Codex 凭据文件格式无效")
        return raw

    def _write_document(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if os.name == "nt":
            payload = _protect(payload)
        fd, name = tempfile.mkstemp(prefix="codex-auth-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def workspace_store(workspace: Path) -> CredentialStore:
    return CredentialStore(workspace / ".desktop" / "codex-auth.bin")
