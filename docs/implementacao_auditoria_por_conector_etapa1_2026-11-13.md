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

Arquivos alterados:

- `app/main.py`
- `app/routes/imports.py`
- `tests/test_connector_audit.py`
- `docs/implementacao_auditoria_por_conector_etapa1_2026-11-13.md`

Implementado:

- `connector_run_start` e `connector_run_finish` dentro do loop, um par por conector;
- nome, modo e origem próprios preservados para cada conector no mesmo disparo;
- falha de um conector registrada sem impedir a tentativa e a auditoria dos demais;
- falhas da auditoria de início ou conclusão protegidas e enviadas ao log da
  aplicação, sem interromper o loop;
- marcação de origem dos `ConnectorRun` mantida em uma única chamada após o loop,
  preservando o padrão anterior de sessão/commit da importação;
- resultado explícito na mensagem de conclusão, mantendo o status normalizado da
  coluna `status`.

Validação antes do commit:

- testes focados: 7 aprovados;
- suíte completa: 367 aprovados, 4 avisos de depreciação preexistentes do FastAPI;
- `compileall` será repetido após o commit, junto com a suíte completa.

## Conferência de Joan após reiniciar em janela segura

1. Executar manualmente um conector e confirmar em **Logs de Auditoria** duas linhas
   com seu nome: `Importação de conector: início` e conclusão/falha; conferir modo,
   origem manual, totais de tabelas, linhas e duração.
2. Após um horário com dois conectores, confirmar pares distintos por nome, com
   origem agendada e o modo próprio de cada conector.
3. Conferir que uma falha de conector aparece como falha e não elimina os registros
   dos outros conectores do mesmo disparo.
4. Conferir que registros históricos `glpi_import_full` e
   `glpi_import_incremental` continuam visíveis como
   `Importação de conector (legado)`, inclusive na exportação CSV e no filtro
   `Sistema`.
5. Salvar uma agenda sem mudar os valores e conferir a mensagem com nome e
   antes/depois de incremental e FULL; os valores persistidos devem permanecer
   idênticos.

O serviço não foi reiniciado nesta implementação. Joan deve reiniciá-lo somente na
janela segura planejada.
