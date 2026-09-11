# Fix — Renomear caminho de instalação

Data: 2026-09-11

## Arquivos alterados

- `install.sh`
- `README.md`
- `docs/arcreports.service.example`
- `app/main.py`
- `app/routes/system_health.py`
- `templates/admin_system_health.html`
- `docs/impl_install_hibrido_2026-09-10.md`
- `docs/impl_readme_install_2026-09-10.md`
- `docs/migrate_passwords_encrypt.py`
- `docs/validation_readme_install_pytest_2026-09-10.txt`
- `docs/fix_rename_path_2026-09-11.md`

`docs/nginx.example.conf` e `AGENTS.md` foram verificados e não continham o caminho de instalação legado.

## Ocorrências substituídas

- 19 ocorrências do caminho legado foram removidas de 10 arquivos versionados.
- `INSTALL_DIR` passou a usar `/opt/sites/arcreports`.
- Os comandos de clone e acesso, em PT-BR e EN, passaram a usar `/opt/sites/arcreports`.
- O exemplo de serviço, as instruções de snapshots e o script documentado de migração passaram a usar `/opt/sites/arcreports`.
- `app/main.py` e `app/routes/system_health.py` agora calculam a raiz a partir de `__file__`, permitindo validar o checkout legado sem criar ou mover o diretório real.

## Resultado

- compileall: ok
- pytest: erro — 98 passed, 1 skipped antes de bloquear de forma reproduzível em `tests/test_export_auth.py::test_export_allowed_with_permission[direct]`; a pilha aponta espera no worker assíncrono AnyIO ao consumir o `StreamingResponse` na linha 74, fora do código alterado nesta tarefa
- bash -n: ok
- grep residual: vazio, tanto no escopo solicitado quanto em todos os arquivos rastreados pelo Git

A suíte completa foi iniciada ao menos três vezes com `python -m pytest tests/ -q` e bloqueou sempre no mesmo ponto. Uma execução focada com `faulthandler_timeout=5` confirmou a espera no adaptador assíncrono. Os processos de teste bloqueados foram interrompidos sem alterar serviços, bancos ou configuração do sistema.
