"""Where a saved sign-in's password lives: the operating system's credential store, and nowhere else.

One small interface (`get`, `set`, `delete` by a key) with these stores:

- `KeyringStore`: the `keyring` package when it is installed (macOS Keychain, Windows Credential Manager, Secret
  Service on Linux);
- `MacKeychainStore`: macOS without `keyring`, through the system's `security` tool. The secret is written on the
  tool's standard input (its interactive mode), never in a command line or the environment, where other processes
  could read it. It is kept base64-encoded, so that no character of a password can be read as part of a command;
- `MemoryStore`: for tests (nothing leaves the process).

Nothing here logs, prints or puts a secret into an exception: errors say what failed, never with what."""

from __future__ import annotations

import base64
import subprocess
import sys

SERVICE = "provenance-core web sign-in"
MAX_SECRET = 512


class CredentialError(Exception):
    """The store could not do it; the text never contains a secret."""


class MemoryStore:
    name = "memory"

    def __init__(self):
        self.items: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def set(self, key: str, secret: str):
        self.items[key] = secret

    def delete(self, key: str):
        self.items.pop(key, None)


class KeyringStore:
    name = "keyring"

    def __init__(self, keyring, service: str = SERVICE):
        self.keyring, self.service = keyring, service

    def get(self, key: str) -> str | None:
        try:
            return self.keyring.get_password(self.service, key)
        except Exception:  # noqa: BLE001 - the backend's own error may quote things; say only what failed
            raise CredentialError("the system credential store could not be read") from None

    def set(self, key: str, secret: str):
        try:
            self.keyring.set_password(self.service, key, secret)
        except Exception:  # noqa: BLE001
            raise CredentialError("the system credential store did not take the password") from None

    def delete(self, key: str):
        try:
            self.keyring.delete_password(self.service, key)
        except Exception:  # noqa: BLE001 - not there, or the backend failed: either way nothing more to do here
            pass


class MacKeychainStore:
    """The login Keychain through /usr/bin/security. Items are generic passwords under `service`, one per key."""

    name = "macos-keychain"
    TOOL = "/usr/bin/security"

    def __init__(self, service: str = SERVICE):
        self.service = service

    @staticmethod
    def _safe(name: str) -> str:
        # names go into a command read from stdin: only characters that cannot end or start anything there
        if not name or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-/@:()" for c in name
        ):
            raise CredentialError("that name cannot be used in the credential store")
        return name

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.TOOL, *args], input=stdin, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise CredentialError("the macOS Keychain tool could not be run") from None

    def get(self, key: str) -> str | None:
        r = self._run(["find-generic-password", "-s", self._safe(self.service), "-a", self._safe(key), "-w"])
        if r.returncode != 0:
            return None
        try:
            return base64.urlsafe_b64decode(r.stdout.strip().encode()).decode()
        except (ValueError, UnicodeDecodeError):
            return None

    def set(self, key: str, secret: str):
        enc = base64.urlsafe_b64encode(secret.encode()).decode()  # [A-Za-z0-9_=-] only
        line = f'add-generic-password -U -s "{self._safe(self.service)}" -a "{self._safe(key)}" -w {enc}\n'
        self._run(["-i"], stdin=line)
        if self.get(key) != secret:
            raise CredentialError("the macOS Keychain did not take the password")

    def delete(self, key: str):
        self._run(["delete-generic-password", "-s", self._safe(self.service), "-a", self._safe(key)])


_store = None
_looked = False


def use(store):
    """Set the store to use (tests: a MemoryStore), or None to pick the system's again."""
    global _store, _looked
    _store, _looked = store, store is not None


def default():
    """The system's credential store, or None when this system has none this app can use."""
    global _store, _looked
    if _looked:
        return _store
    _looked = True
    try:
        import keyring  # noqa: PLC0415

        _store = KeyringStore(keyring)
    except ImportError:
        _store = MacKeychainStore() if sys.platform == "darwin" else None
    return _store
