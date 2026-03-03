"""机器指纹 + 凭据加密存储"""
import hashlib, json, os, platform, uuid
from pathlib import Path

CRED_FILE = Path.home() / ".qtsys_dbmanager.json"


def get_machine_id() -> str:
    raw = f"{platform.node()}-{uuid.getnode()}-{platform.system()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _get_key() -> bytes:
    from cryptography.fernet import Fernet
    import base64
    mid = get_machine_id().encode()
    key = hashlib.sha256(mid).digest()
    return base64.urlsafe_b64encode(key)


def save_credentials(host, port, user, password, database):
    from cryptography.fernet import Fernet
    f = Fernet(_get_key())
    data = {
        "machine_id": get_machine_id(),
        "host": host, "port": port,
        "user": user, "database": database,
        "password": f.encrypt(password.encode()).decode(),
    }
    CRED_FILE.write_text(json.dumps(data, indent=2))


def load_credentials() -> dict | None:
    if not CRED_FILE.exists():
        return None
    try:
        from cryptography.fernet import Fernet
        data = json.loads(CRED_FILE.read_text())
        if data.get("machine_id") != get_machine_id():
            return None
        f = Fernet(_get_key())
        data["password"] = f.decrypt(data["password"].encode()).decode()
        return data
    except Exception:
        return None
