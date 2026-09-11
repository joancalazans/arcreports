# Implementação — Cancelar ETL + FULL Personalizado
Data: 2026-07-22

## Mecanismo de cancelamento
`threading.Event` por `connector_type`, com funções get/request/clear/is_cancelled.

## Verificação no loop ETL
Verificado antes e após cada tabela em `run_connector_import`; a execução marca a tabela atual como `cancelled` após concluir.

## Botão Cancelar
Aparece somente em cards ativos com `ConnectorRun.status='running'`.

## FULL Personalizado
Cards com `import_mode='custom'` exibem “FULL Personalizado”; o backend mantém a whitelist.

## Resultado
- compileall: pendente
- pytest: pendente
