from __future__ import annotations

import os
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from .config import SynapseConfig

ENV_KEY = "SYNAPSE_FERNET_KEY"
MARKER = "SYNAPSE-FERNET\n"


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def get_fernet(config: SynapseConfig) -> Fernet:
    key = _load_key(config)
    return Fernet(key.encode("ascii"))


def encrypt_text(config: SynapseConfig, text: str) -> str:
    encrypted = get_fernet(config).encrypt(text.encode("utf-8")).decode("ascii")
    return f"{MARKER}{encrypted}"


def decrypt_text(config: SynapseConfig, text: str) -> str:
    if not text.startswith(MARKER):
        return text
    token = text.removeprefix(MARKER).strip().encode("ascii")
    return get_fernet(config).decrypt(token).decode("utf-8")


def read_text(config: SynapseConfig, path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return decrypt_text(config, text) if config.encryption else text


def write_text(config: SynapseConfig, path: Path, text: str) -> None:
    path.write_text(encrypt_text(config, text) if config.encryption else text, encoding="utf-8")


def encrypted_export(config: SynapseConfig, output_path: Path, password: str) -> Path:
    # ZipCrypto is used by the stdlib. This is a compatibility export, not a replacement
    # for Fernet-at-rest encryption.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.setpassword(password.encode("utf-8"))
        for path in config.vault_path.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(config.vault_path))
    return output_path


def _load_key(config: SynapseConfig) -> str:
    key = os.getenv(ENV_KEY)
    env_path = config.root_path / ".env"
    if not key and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{ENV_KEY}="):
                key = line.split("=", 1)[1].strip().strip('"')
                break
    if not key:
        raise ValueError(f"Encryption enabled but {ENV_KEY} is not set in environment or .env")
    return key
