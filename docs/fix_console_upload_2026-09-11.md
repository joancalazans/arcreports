# Fix — Console SQL e upload de imagens
Data: 2026-09-11

## Console SQL
- `trs_sla_contratos` removido de `ALLOWED_WRITE_TABLES`
- `ALLOWED_WRITE_PREFIXES` generalizado com `sti_`, `zabbix_`, `redmine_`, `bookstack_`, `custom_`
- Template confirmado usando `allowed_write_tables` e `allowed_write_prefixes` do contexto

## Upload de imagens
- Fundo login: limite aumentado para 7MB
- Logo PDF: limite 2MB mantido
- Extensões aceitas: jpg, jpeg, png
- Validação backend com `validate_image_upload()`
- Validação frontend com alert amigável
- Textos de orientação com resolução recomendada
- `nginx.example.conf`: `client_max_body_size 10m`
- README: nota sobre `client_max_body_size`

## Resultado
- compileall: ok
- pytest: 342 passed
