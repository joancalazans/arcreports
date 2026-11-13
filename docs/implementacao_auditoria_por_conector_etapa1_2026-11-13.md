# Etapa 1 — auditoria por conector

Data: 13/11/2026. Escopo restrito a código, testes e este resumo. Nenhuma migração,
alteração de schema/dados, mudança de agendamento/lock/importação, reinício de serviço
ou push foi realizado.

## Snapshot anterior às alterações

Foi confirmado o snapshot automático do dia já existente:
`snapshots/auto_20261113_050000.sql`, 388.804.887 bytes, criado às 02:00 no horário
local. Não foi criado dump adicional no host de produção.

## Mapeamento prévio das ações

- `glpi_import_full` e `glpi_import_incremental`: eram geradas exclusivamente pelo
  helper `record_connector_import_admin_log()` em `app/routes/imports.py`; o helper
  era chamado pelo background manual no mesmo arquivo e pelo dispatcher em
  `app/main.py`.
- `connector_run_start`: gerada exclusivamente pela rota manual em
  `app/routes/imports.py`.
- `glpi_schedule_save`: gerada exclusivamente pela rota de salvar agenda em
  `app/routes/imports.py`.
- Não havia ocorrências adicionais dessas quatro ações em `templates/` ou `tests/`.
- `app/routes/history.py` centraliza rótulos em `format_action()`, categorias em
  `admin_category()` e alimenta tela e exportação CSV pela mesma função
  `build_audit_log_query()`. A categoria atual é `Sistema`; filtros e exportação não
  filtram essas ações por uma lista fechada.

## Commit 1 — helper, manual, histórico, contagem e agenda

Arquivos alterados:

- `app/routes/imports.py`
- `app/routes/history.py`
- `tests/test_connector_audit.py`
- `docs/implementacao_auditoria_por_conector_etapa1_2026-11-13.md`

Implementado:

- ação de conclusão `connector_run_finish`, preservando o nome do helper para
  compatibilidade;
- mensagem com nome, tipo, modo, origem, tabelas com sucesso/erro/puladas, linhas e
  duração, limitada ao tamanho máximo em bytes de `TEXT`;
- contagem pelo conjunto exato de `ConnectorRun` retornado pela importação; fallback
  compatível por conector a partir do segundo truncado menos um segundo;
- auditoria manual protegida para que falhas do helper ou do registro do erro não
  alterem a conclusão do background nem deixem o lock preso;
- rótulos PT-BR das ações novas, incluindo nome do conector, e leitura compatível de
  `glpi_import_full`/`glpi_import_incremental` como
  `Importação de conector (legado)`;
- mensagem de agenda com nome e valores antes/depois para incremental e FULL, sem
  modificar validação ou valores persistidos.

Validação antes do commit:

- `python -m compileall -q app tests`: concluído sem erros;
- `pytest -q -p no:cacheprovider`: 364 testes aprovados, 4 avisos de depreciação
  preexistentes do FastAPI;
- o teste de integração somente leitura precisou ser executado fora do sandbox para
  acessar o MariaDB local; nenhum dado foi alterado.

Também foram corrigidas duas expectativas antigas de testes: data fixa já fora da
retenção do histórico e exemplos com nomes específicos substituídos por nomenclatura
neutra já aceita pelo código.

## Commit 2 — dispatcher agendado

A preencher no segundo commit.

## Conferência de Joan após reiniciar em janela segura

A preencher ao concluir o segundo commit.
