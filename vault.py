"""Encrypted storage for career-portal passwords.

How it protects you:
  * Passwords are encrypted (Fernet: AES-128-CBC + HMAC) with a key derived from YOUR master
    password using scrypt. The master password itself is never stored, only a salt and an
    encrypted check value used to tell whether the password you typed is right.
  * Anyone who copies jobs.db gets ciphertext only. Without the master password it is useless.
  * The unlocked key lives in the dashboard's memory only, and is dropped after 15 idle minutes
    or when you press Lock.
  * Wrong guesses are rate-limited (5 tries, then a 60-second pause).

If you forget the master password the stored passwords CANNOT be recovered. That is the point.
A dedicated password manager (1Password, Bitwarden, Apple Passwords) is still the safer home for
important accounts; this vault exists so the agent can sign in to career portals for you later.
"""
import base64
import hashlib
import os
import time

from cryptography.fernet import Fernet, InvalidToken

MIN_LEN = 10
IDLE_SECONDS = 15 * 60
MAX_FAILS = 5
PAUSE_SECONDS = 60


class VaultError(Exception):
    pass


def _derive(master: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(master.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1,
                         dklen=32, maxmem=128 * 1024 * 1024)
    return base64.urlsafe_b64encode(raw)


class Vault:
    def __init__(self):
        self._key = None
        self._last = 0.0
        self._fails = 0
        self._blocked_until = 0.0

    # --- stored metadata -------------------------------------------------
    @staticmethod
    def _meta(db, k):
        row = db.execute("SELECT value FROM vault_meta WHERE key=?", (k,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set(db, k, v):
        db.execute("INSERT OR REPLACE INTO vault_meta(key, value) VALUES (?,?)", (k, v))
        db.commit()

    def initialized(self, db) -> bool:
        return self._meta(db, "salt") is not None

    # --- lifecycle ---------------------------------------------------------
    def setup(self, db, master: str, confirm: str):
        if self.initialized(db):
            raise VaultError("The vault is already set up.")
        if master != confirm:
            raise VaultError("The two passwords do not match.")
        if len(master) < MIN_LEN:
            raise VaultError(f"Use at least {MIN_LEN} characters.")
        salt = os.urandom(16)
        key = _derive(master, salt)
        self._set(db, "salt", base64.b64encode(salt).decode())
        self._set(db, "verifier", Fernet(key).encrypt(b"vault-ok").decode())
        self._key, self._last = key, time.time()

    def unlock(self, db, master: str) -> bool:
        if not self.initialized(db):
            raise VaultError("Set up the vault first.")
        if time.time() < self._blocked_until:
            raise VaultError("Too many wrong attempts. Wait a minute and try again.")
        key = _derive(master, base64.b64decode(self._meta(db, "salt")))
        try:
            Fernet(key).decrypt(self._meta(db, "verifier").encode())
        except InvalidToken:
            self._fails += 1
            if self._fails >= MAX_FAILS:
                self._fails, self._blocked_until = 0, time.time() + PAUSE_SECONDS
            return False
        self._fails = 0
        self._key, self._last = key, time.time()
        return True

    def lock(self):
        self._key = None

    def is_unlocked(self) -> bool:
        if self._key and time.time() - self._last > IDLE_SECONDS:
            self._key = None
        return self._key is not None

    def touch(self):
        if self._key:
            self._last = time.time()

    # --- use ---------------------------------------------------------------
    def encrypt(self, text: str) -> str:
        if not self.is_unlocked():
            raise VaultError("The vault is locked.")
        return Fernet(self._key).encrypt(text.encode("utf-8")).decode()

    def decrypt(self, token: str) -> str:
        if not self.is_unlocked():
            raise VaultError("The vault is locked.")
        try:
            return Fernet(self._key).decrypt(token.encode()).decode("utf-8")
        except InvalidToken:
            raise VaultError("Could not decrypt this entry.")
