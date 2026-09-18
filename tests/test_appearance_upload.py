import pytest

from app.routes.appearance import validate_image_upload


@pytest.mark.parametrize("filename", ["fundo.jpg", "fundo.JPEG", "logo.png"])
def test_validate_image_upload_accepts_supported_extensions(filename):
    assert validate_image_upload(filename, b"image", 1024, "Imagem") is None


def test_validate_image_upload_rejects_unsupported_extension():
    error = validate_image_upload("imagem.webp", b"image", 1024, "Imagem")

    assert error == "Extensão não permitida: .webp. Use: JPG, JPEG ou PNG."


def test_validate_image_upload_rejects_oversized_file():
    error = validate_image_upload(
        "imagem.png",
        b"x" * (2 * 1024 * 1024 + 1),
        2 * 1024 * 1024,
        "Logo da empresa",
    )

    assert error == "Arquivo muito grande: 2.0MB. Máximo permitido: 2MB."
