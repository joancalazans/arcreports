# Testes

## Instalar pytest

```bash
pip install pytest
```

## Rodar todos os testes

```bash
cd /opt/sites/GLPI_Portal
python -m pytest tests/ -v
```

## Rodar apenas um arquivo

```bash
python -m pytest tests/test_validate_sql.py -v
```

## Rodar com cobertura

Se `pytest-cov` estiver instalado:

```bash
python -m pytest tests/ -v --cov=app
```

## Resultado esperado

Todos os testes devem passar sem erro.
