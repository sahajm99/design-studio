"""Saved provider keys (v6): encrypted at rest, never sent back, redacted from every error.

Keys saved on the Settings page are kept as one Fernet token (authenticated symmetric
encryption from `cryptography`) in the settings table under `provider_keys`. The secret that
encrypts them is `STUDIO_SECRET_KEY` from `.env`; without one, the studio makes a secret on
first start and keeps it beside the database, in the database volume. A secret kept there
protects keys in a copied or backed-up `studio.db`, not on a machine someone else controls;
`STUDIO_SECRET_KEY` in `.env` keeps the two apart.

A secret that cannot read the saved keys never stops the studio: the saved keys are treated
as absent, the Settings page says so, and the keys in `.env` still work.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError

from studio.config import Settings
from studio.store import Store

logger = logging.getLogger(__name__)

# The settings key that holds the encrypted keys, and the secret's file beside the database.
PROVIDER_KEYS_SETTING = "provider_keys"
SECRET_FILE = "secret.key"
# The shortest text `redact` removes: anything shorter is not a key and would mangle the text.
_MIN_SECRET_CHARS = 8
# How much of a saved key stays in view.
_SHOWN_CHARS = 4

MADE_SECRET = (
    "Made a secret for saved keys in the database volume. "
    "Set STUDIO_SECRET_KEY to keep it in .env instead."
)
KEYS_UNREADABLE = "Saved keys can't be read with this secret. Enter them again."
SECRET_INVALID = (
    "STUDIO_SECRET_KEY is not a Fernet key, so saved keys are off until it is one. "
    ".env.example says how to make one."
)
SECRET_FILE_INVALID = "The secret for saved keys in the database volume is damaged."
SECRET_FILE_FAILED = (
    "The secret for saved keys could not be read or written in the database volume."
)
NO_SECRET = "Saved keys are off: the studio has no secret to encrypt them with."
REDACTED = "[key removed]"


class ProviderKeys(BaseModel):
    """Each provider's key in plain form. Only ever in memory: stored, it is encrypted."""

    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    openai_api_key: str = ""
    google_image_api_key: str = ""

    def values(self) -> list[str]:
        """Every key that is set, for `redact`."""
        return [value for value in self.model_dump().values() if value]


class EncryptedKeys(BaseModel):
    """The `provider_keys` setting: the keys as one Fernet token."""

    token: str


class SecretUnavailable(RuntimeError):
    """The studio has no usable secret, so a key cannot be saved."""


def load_secret(settings: Settings) -> bytes | None:
    """The secret for saved keys: `STUDIO_SECRET_KEY`, else the one kept beside the database,
    made now when there is none. None, after a warning in the log, when the configured secret
    is not a Fernet key or the volume's secret cannot be read or written."""
    if settings.secret_key:
        secret = settings.secret_key.encode("ascii", errors="replace")
        if _is_fernet_key(secret):
            return secret
        logger.warning(SECRET_INVALID)
        return None
    path = settings.db_path.parent / SECRET_FILE
    try:
        if path.is_file():
            secret = path.read_bytes().strip()
            if _is_fernet_key(secret):
                return secret
            logger.warning(SECRET_FILE_INVALID)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = Fernet.generate_key()
        # Readable by the studio's own user only, and never written over.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secret)
    except OSError:
        logger.warning(SECRET_FILE_FAILED)
        return None
    logger.warning(MADE_SECRET)
    return secret


def read_saved_keys(store: Store, secret: bytes | None) -> tuple[ProviderKeys, bool]:
    """The saved keys, and whether saved keys exist that this secret cannot read. With nothing
    saved, the keys are empty and readable."""
    data = store.get_setting(PROVIDER_KEYS_SETTING)
    if data is None:
        return ProviderKeys(), False
    if secret is None:
        return ProviderKeys(), True
    try:
        token = EncryptedKeys.model_validate(data).token
        plain = Fernet(secret).decrypt(token.encode("ascii"))
        return ProviderKeys.model_validate_json(plain), False
    except (InvalidToken, ValidationError, ValueError):
        logger.warning(KEYS_UNREADABLE)
        return ProviderKeys(), True


def save_provider_keys(store: Store, secret: bytes | None, keys: ProviderKeys) -> None:
    """Encrypt the keys and save them under `provider_keys`; with no key set, remove the row.
    Raises SecretUnavailable when there is no secret to encrypt them with."""
    if not keys.values():
        store.set_setting(PROVIDER_KEYS_SETTING, None)
        return
    if secret is None:
        raise SecretUnavailable(NO_SECRET)
    token = Fernet(secret).encrypt(keys.model_dump_json().encode("utf-8")).decode("ascii")
    store.set_setting(PROVIDER_KEYS_SETTING, EncryptedKeys(token=token))


def last_four(key: str) -> str:
    """The end of a key that stays in view, "7f3a"; nothing for a key too short to show part of."""
    return key[-_SHOWN_CHARS:] if len(key) >= 2 * _SHOWN_CHARS else ""


def redact(text: str, secrets: Iterable[str]) -> str:
    """The text with every known key in it replaced, longest first, so an error message can be
    logged and stored. Text too short to be a key is left alone."""
    known = sorted({value for value in secrets if len(value) >= _MIN_SECRET_CHARS}, key=len)
    for value in reversed(known):
        text = text.replace(value, REDACTED)
    return text


def _is_fernet_key(secret: bytes) -> bool:
    try:
        Fernet(secret)
    except (ValueError, TypeError):
        return False
    return True
