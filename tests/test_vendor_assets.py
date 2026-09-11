from pathlib import Path
import re


def test_no_external_cdn_in_templates():
    """Nenhum template contém referência a CDN externo."""
    cdn_pattern = re.compile(
        r"(cdn\.|jsdelivr|unpkg|cdnjs|cloudflare\.com|googleapis)",
        re.IGNORECASE,
    )
    templates = Path(__file__).resolve().parents[1] / "templates"
    paths = list(templates.rglob("*.html"))
    assert paths, "Nenhum template encontrado"
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert not cdn_pattern.search(content), f"CDN externo encontrado em {path}"
