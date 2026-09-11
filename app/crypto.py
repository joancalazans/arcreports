from base64 import urlsafe_b64encode

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# Salt fixo derivado do nome do produto. Nao e segredo; apenas torna a chave
# derivada especifica para a criptografia de senhas de conectores do ArcReports.
_SALT = b"arcreports-connector-passwords-v1"


def _derive_key(secret_key: str) -> bytes:
    """Deriva uma chave Fernet a partir do SECRET_KEY."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=100_000,
    )
    return urlsafe_b64encode(kdf.derive(secret_key.encode()))


def get_fernet(secret_key: str) -> Fernet:
    return Fernet(_derive_key(secret_key))


def encrypt_password(plain: str, secret_key: str) -> str:
    """Criptografa uma senha e retorna o token Fernet em texto base64."""
    if not plain:
        return plain
    return get_fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str, secret_key: str) -> str:
    """Descriptografa uma senha, aceitando texto claro legado."""
    if not encrypted:
        return encrypted
    if not encrypted.startswith("gAAAAA"):
        return encrypted
    try:
        return get_fernet(secret_key).decrypt(encrypted.encode()).decode()
    except Exception:
        return encrypted


def is_encrypted(value: str) -> bool:
    """Verifica se o valor possui o prefixo padrao de um token Fernet."""
    return bool(value and value.startswith("gAAAAA"))
