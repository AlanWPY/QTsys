from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import RUNTIME_DIR
from logging_config import get_logger


logger = get_logger("qtsys.secret_crypto")

SECRET_PREFIX = "enc:v1:"
MASTER_KEY_ENV = "QTSYS_MASTER_KEY"
MASTER_KEY_FILE = Path(RUNTIME_DIR) / "secrets" / "master.key"


def is_encrypted_secret(value: str | None) -> bool:
    return bool(value) and str(value).startswith(SECRET_PREFIX)


def _normalize_master_key(raw: str) -> bytes:
    key = raw.strip().encode("utf-8")
    try:
        Fernet(key)
        return key
    except Exception as exc:
        raise ValueError("QTSYS_MASTER_KEY 不是合法的 Fernet 密钥") from exc


def _load_or_create_master_key() -> bytes:
    env_key = os.getenv(MASTER_KEY_ENV, "").strip()
    if env_key:
        return _normalize_master_key(env_key)

    MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if MASTER_KEY_FILE.exists():
        return _normalize_master_key(MASTER_KEY_FILE.read_text(encoding="utf-8"))

    key = Fernet.generate_key()
    MASTER_KEY_FILE.write_text(key.decode("utf-8"), encoding="utf-8")
    try:
        os.chmod(MASTER_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info("已生成本地主密钥文件：%s", MASTER_KEY_FILE)
    return key


@lru_cache(maxsize=1)
def get_fernet() -> Fernet:
    return Fernet(_load_or_create_master_key())


def encrypt_secret(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if is_encrypted_secret(text):
        return text
    token = get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")
    return f"{SECRET_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if not is_encrypted_secret(text):
        return text
    token = text[len(SECRET_PREFIX):].encode("utf-8")
    try:
        return get_fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, base64.binascii.Error):
        logger.error("检测到无法解密的敏感配置，请确认 QTSYS_MASTER_KEY 与本地主密钥文件一致")
        return ""
