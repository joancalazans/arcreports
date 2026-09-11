from app.crypto import decrypt_password, encrypt_password, is_encrypted


def test_encrypt_decrypt_roundtrip():
    plain = "senha_teste_123"
    secret = "chave-secreta-de-teste-32-chars!!"
    encrypted = encrypt_password(plain, secret)
    assert encrypted != plain
    assert is_encrypted(encrypted)
    assert decrypt_password(encrypted, secret) == plain


def test_decrypt_plain_passthrough():
    """Senha nao criptografada deve passar sem erro."""
    plain = "senha_legada"
    secret = "chave-secreta-de-teste-32-chars!!"
    assert decrypt_password(plain, secret) == plain


def test_encrypt_empty():
    assert encrypt_password("", "qualquer") == ""
    assert decrypt_password("", "qualquer") == ""


def test_is_encrypted():
    secret = "chave-secreta-de-teste-32-chars!!"
    encrypted = encrypt_password("teste", secret)
    assert is_encrypted(encrypted)
    assert not is_encrypted("texto_plano")
    assert not is_encrypted("")
