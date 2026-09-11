# ArcReports — README, exemplos de ambiente e instalador

Referência solicitada: 2026-09-10. Conclusão da sessão: 2026-09-11 (America/Sao_Paulo).
Produto: ArcReports v0.1.0, AGPL-3.0.

## Arquivos entregues

- [README.md](../README.md): Português como padrão e tradução completa em inglês, com âncoras HTML explícitas e seletor visual. Inclui todas as seções solicitadas, arquitetura textual, contas, horários, segurança, backup e desenvolvimento.
- [.env.example](../.env.example): todas as 20 chaves de configuração identificadas por leitura estática de `app/config.py`, incluindo `env_bool`, mais `SESSION_TTL_HOURS`, consumida em `app/security.py`.
- [local.env.example](../local.env.example): `ADMIN_USERNAME` e `ADMIN_PASSWORD` com orientação de bootstrap.
- [ldap.env.example](../ldap.env.example): oito chaves LDAP, todas comentadas.
- [install.sh](../install.sh): preparação híbrida e verificação HTTP separadas; invocação por `bash`, sem necessidade de bit executável.
- [nginx.example.conf](nginx.example.conf): proxy para 127.0.0.1:8000 com hostname público fictício e cabeçalhos explícitos.
- [arcreports.service.example](arcreports.service.example): unidade independente, usuário `ia-dev`, um worker e caminhos do produto.
- Este relatório e quatro registros de validação em `docs/`.

## Base consultada

Foram lidos os documentos solicitados: `raio_x_ambiente_2026-09-10.md`, `impl_alembic_2026-09-10.md`, `alembic_workflow.md`, `fix_vendor_assets_2026-09-10.md`, `fix_export_auth_2026-09-10.md`, `impl_snapshots_2026-08-25.md`; além de `app/config.py`, `app/models.py`, `requirements.txt` e `pyproject.toml`.

Complementos estáticos: `docs/fix_alembic_orm_2026-09-10.md`, ambiente/revisão Alembic, bootstrap e agendamentos em `app/main.py`, autenticação, LDAP, snapshots e testes. Correções posteriores prevaleceram sobre o diagnóstico antigo: assets já são locais e exportações já verificam categorias. Dados internos do inventário não foram copiados para os exemplos.

## Decisões e diferenças necessárias em relação ao esboço

1. **Regras absolutas e opção híbrida.** O instalador nunca executa como root e não cria usuário Linux, altera Nginx/systemd ou instala pacotes do SO. Essas etapas são pré-requisitos ou ações manuais do operador. Na execução futura, ele exportará `docs/install_operator_steps.txt` com criação/preparação de conta e diretório, cópia do código revisado, atualização de configurações e comandos de ativação. O arquivo não foi gerado por uma execução do instalador nesta tarefa. Não há cópia automática para `/etc`. Essa separação preserva as regras absolutas diante das etapas privilegiadas do esboço.
2. **Código da versão.** O operador disponibiliza o checkout/release previamente; não há clone de URL não homologada nem atualização automática por `git pull`. Configurações, uploads, logs e snapshots devem ser preservados ao atualizar. Caminho fixo `/opt/sites/arcreports` acompanha os caminhos absolutos atuais do core.
3. **Privilégios do banco.** A preparação futura usa os grants exatos solicitados em `reports`, com criação condicional de database e contas. Os grants são aditivos: não revogam privilégios antigos. Senhas existentes não são redefinidas e são verificadas antes dos grants. O bootstrap exige credencial DBA local não root já disponível em `INSTALL_DB_CNF`, arquivo INI privado modo 0600, ou a conta administrativa de `.env` existente. Esse pré-requisito permite criar as três contas sem solicitar uma quarta senha nem recorrer a root. Não é possível criar contas privilegiadas partindo de uma conta que ainda não existe.
4. **Preservação de segredos.** A chave é gerada apenas na primeira criação de `.env`. Arquivo existente sem chave válida interrompe a preparação e exige recuperação da chave original, inclusive considerando o alias `APP_SECRET_KEY`. A confirmação de escrita em reinstalação permite somente completar chaves ausentes, com backup privado; todos os valores existentes são preservados. `local.env` e `ldap.env` existentes não são substituídos. O script não usa `source` ou `eval` para credenciais; usa python-dotenv e escrita atômica. Valores `${...}` são recusados para evitar interpolação, e senhas novas exigem 16 caracteres.
5. **Schema e Alembic.** A baseline histórica contém alterações incrementais, não CREATE TABLE de toda a aplicação. Banco vazio recebe `Base.metadata.create_all` sem importar `app.main` ou iniciar jobs. Em banco sem revisão, a comparação do schema com o ORM deve ser vazia antes de `stamp head`, permitido somente quando head é `74d249fd4a23`. Migrações pendentes em banco versionado exigem o workflow revisado do operador, com backup; nunca são ocultadas por um novo stamp. O script termina verificando `alembic check`. Não foram executados esses caminhos de banco na sessão.
6. **Saúde HTTP.** `/health` não existe no core atual e a regra proíbe alterar a aplicação. `--verify` verifica esse endpoint e, somente em 404, exige 200 de `/login`. A preparação não anuncia instalação ativa: o resumo de sucesso vem somente após verificação HTTP separada. Login, ETL, banco e exportações ainda precisam de validação funcional.
7. **Versões.** Nove casos cobrem comparação numérica de componentes, mínimos insuficientes e saída inválida. MariaDB compara `Distrib 10.x`, não o `Ver 15.1` do cliente, e o provisionamento futuro também verifica a versão do servidor. Não houve sondagem dos serviços reais nesta tarefa.
8. **Exemplos de infraestrutura.** A unidade foi escrita a partir dos requisitos da aplicação, sem ler o arquivo real em `/etc`. Ela não usa `EnvironmentFile`: python-dotenv carrega os três arquivos e evita divergências de escaping com systemd. O exemplo Nginx sobrescreve `X-Forwarded-For` para clientes diretamente conectados, preservando um IP confiável para o rate limit. Hostname, TLS e proxies adicionais requerem revisão do operador. Configurações existentes devem ser atualizadas sem duplicar virtual host ou serviço.
9. **Offline e backups.** README distingue uso sem CDN de instalação de dependências e acesso de rede às origens/LDAP. Snapshots cobrem `reports`, não réplicas, configurações, uploads, contas ou código; é necessário backup externo. A retenção não garante dias nem preservação de pelo menos um arquivo. Os grants solicitados podem ser insuficientes para `mysqldump --routines --triggers`; a homologação deve ser feita pelo DBA, sem concessões adicionais implícitas.
10. **Atualização.** O serviço deve estar parado pelo operador antes da preparação. O instalador conserva configurações, verifica schema e instala dependências do requirements; não aplica migrações novas sem revisão e não oferece rollback automático. Instalação limpa, atualização ponta a ponta e restore não foram homologados por execução real nesta tarefa.

## Validação

Todas as leituras, gravações e validações de projeto foram executadas como `ia-dev` por troca de identidade autorizada. O sandbox inicialmente impediu a troca de usuário em uma gravação; a execução foi refeita com a permissão de `runuser`. Nenhum comando de aplicação foi executado como root.

| Verificação | Resultado |
|---|---|
| `python -m compileall app/ -q` | OK, bytecode direcionado a `/tmp` |
| `python -m pytest tests/ -q` com plugin de isolamento | 327 passed, 1 skipped, 4 warnings, 12,44 s |
| `bash -n install.sh` | OK |
| Compilação dos dois blocos Python embutidos | OK |
| Comparação de versões | 9 cenários OK |
| Cobertura de configuração | 20 chaves de config.py + SESSION_TTL_HOURS; nenhuma duplicada |
| LDAP | 8 chaves, todas comentadas |
| Links locais do README | OK |
| Geração de configurações em `/tmp` | Nova instalação, senha com aspas/barra/dólar/espaços, recusa de escrita, complementação com backup, preservação da chave e interrupção por chave ausente: OK |

O plugin temporário bloqueou conexões por socket, PyMySQL e psycopg2 e execução de subprocessos, substituiu handlers de log por handlers sem arquivo e impediu leitura dos três arquivos de credenciais reais. O único teste de integração MariaDB, `test_glpi_tables_exist_only_in_connector_database`, foi marcado como skip antes de executar. SQLite em memória e mocks da suíte foram mantidos. Os quatro avisos são depreciações existentes de `FastAPI.on_event`. A primeira tentativa do harness teve erro de sintaxe antes da coleta; foi corrigido apenas em `/tmp` e a suíte foi repetida com sucesso. Nenhum teste ou código da aplicação foi modificado.

A comparação ingênua com `grep os.getenv` não detecta `SQL_CONSOLE_ENABLED` (passada por `env_bool`) e não inclui `SESSION_TTL_HOURS` (outro arquivo). Por isso a conferência usa AST, verifica cobertura sem duplicação e permite exclusivamente a chave adicional comprovada em `app/security.py`; não remove chaves válidas para forçar um diff vazio.

Evidências:

- [Validação estática](validation_readme_install_static_2026-09-10.txt)
- [Compileall](validation_readme_install_compile_2026-09-10.txt)
- [Pytest](validation_readme_install_pytest_2026-09-10.txt)
- [Preservação de configuração](validation_readme_install_config_2026-09-10.txt)

## Limites da entrega

Nenhuma conexão ou comando foi executado no MariaDB/GLPI. Nenhum serviço foi iniciado, parado, reiniciado ou recarregado. Linux, Nginx, systemd e código da aplicação não foram alterados; os arquivos reais de configuração de serviço/proxy em `/etc` não foram consultados. O instalador completo não foi executado. A homologação de instalação/atualização em ambiente descartável, grants, snapshots e restore permanece uma atividade operacional futura.
