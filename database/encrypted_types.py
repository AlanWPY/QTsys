from __future__ import annotations

from sqlalchemy.types import Text, TypeDecorator

from database.secret_crypto import decrypt_secret, encrypt_secret


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        return decrypt_secret(value)
