# Portal GLPI Reports

## Objetivo
Criar um portal web para relatórios do GLPI.

## Stack
- FastAPI
- Jinja2
- SQLAlchemy
- PyMySQL
- MariaDB
- Bootstrap

## Estrutura
- app/
- templates/
- static/
- etl/

## Regras importantes
- Nunca alterar Linux
- Nunca alterar Nginx
- Nunca alterar systemd
- Nunca usar root
- Nunca executar comandos destrutivos
- Nunca executar DELETE, DROP, UPDATE, ALTER ou TRUNCATE no banco GLPI
- Somente SELECT no banco GLPI
- Salvar resultados localmente no banco glpi_reports

## Banco
Usar variáveis do arquivo .env

## Objetivo inicial
Criar:
- Login
- Dashboard
- Cadastro de relatórios
- Execução manual de SELECT
- Histórico de execução
- Exportação Excel
