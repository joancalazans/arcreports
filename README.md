# ArcReports

<!-- Seletor de idioma -->
[🇧🇷 Português](#português) | [🇺🇸 English](#english)

**v0.1.0 · AGPL-3.0 · ArcData MSP**

---

<a id="português"></a>
## Português

### O que é

Plataforma agnóstica de replicação e análise de dados para BI. Conecta a fontes MySQL/MariaDB ou PostgreSQL, replica localmente e permite criar relatórios e dashboards sem depender de internet. Desenvolvida para equipes de TI e para a futura comunidade open source. O idioma nativo da interface é Português (PT-BR); o seletor deste README muda apenas a seção do documento.

### Funcionalidades

- Conectores: GLPI, Redmine, Zabbix, BookStack e Personalizado.
- Relatórios primários e derivados com materialização local.
- Dashboards com widgets KPI, Gauge, Barra, Pizza e Tabela.
- Filtro temporal com suporte a datas ISO e formato brasileiro.
- Autenticação local + LDAP com fallback para acesso local de contingência.
- Permissões granulares por módulo e categoria.
- Console SQL administrativo.
- Snapshots automáticos diários.
- Exportação PDF de dashboards no navegador e Excel de relatórios.
- Operação 100% offline — zero dependência de CDN nos templates; assets em `static/vendor/`.

Offline significa sem internet externa durante o uso. Replicação exige acesso de rede às origens; LDAP exige acesso ao diretório. A instalação das dependências precisa de internet ou de um repositório local de wheels previamente preparado. Cada conector depende do schema e das permissões da fonte; não implica compatibilidade automática com qualquer aplicação.

### Requisitos mínimos

| Componente | Versão mínima | Testado em |
|---|---|---|
| RHEL / Rocky / AlmaLinux | 9.x | RHEL 9.8 |
| Fedora | 38+ | — |
| Ubuntu | 22.04 LTS+ | — |
| Debian | 11+ | — |
| Python | 3.9 | 3.9.25 |
| MariaDB | 10.5 | 10.5.29 |
| Nginx | 1.18 | 1.20.1 |

A coluna “Testado em” identifica o ambiente inventariado em 10/09/2026; instalação limpa e Rocky/AlmaLinux ainda precisam de homologação. Dimensione RAM e disco pelo volume das réplicas, resultados, índices e snapshots. São necessários `mysql`, `mysqldump`, `curl`, suporte a `venv` e as dependências de [requirements.txt](requirements.txt). O MariaDB local é obrigatório mesmo com origens PostgreSQL.

### Instalação rápida

A instalação é híbrida: o operador prepara a infraestrutura e ativa o serviço; [install.sh](install.sh) automatiza a parte da aplicação e o provisionamento do MariaDB local. O script deve ser executado como root. Ele cria automaticamente o usuário `ia-dev` — dono do projeto, dos arquivos, do venv e do serviço systemd — sem senha de login direto. Para acessar manualmente: `sudo su - ia-dev`. Operações da aplicação (venv, `.env`, Alembic) são executadas internamente como `ia-dev` via `runuser`. O script não configura firewall nem SELinux automaticamente, mas exibe instruções ao final quando detecta firewalld ou ufw ativos.

1. Execute o instalador como root em um servidor com RHEL 9.x, Rocky, AlmaLinux, Fedora 38+, Ubuntu 22.04+ ou Debian 11+. O script verifica e instala Python 3.9+, MariaDB 10.5+ e Nginx 1.18+ via `dnf` ou `apt-get` automaticamente se necessário, cria o usuário `ia-dev` (sem senha de login direto) e prepara o diretório `/opt/sites/glpi-portal`.
2. Prepare uma credencial DBA **não root**, já autorizada no MariaDB local, em arquivo privado de `ia-dev` com modo `0600`. Exemplo de conteúdo (formato INI; senha literal sem aspas):

   ```ini
   [client]
   host=localhost
   port=3306
   user=dba_instalacao
   password=SENHA_FORTE_AQUI
   ```

3. Como root, clone e execute:

   ```bash
   git clone https://github.com/joancalazans/arcreports.git \
     /opt/sites/glpi-portal
   cd /opt/sites/glpi-portal
   sudo bash install.sh
   ```

   Ou com credencial DBA personalizada:

   ```bash
   sudo INSTALL_DB_CNF=/caminho/bootstrap.cnf \
     bash install.sh
   ```

   Informe somente as três senhas de banco, `ADMIN_USERNAME` e `ADMIN_PASSWORD` quando solicitado. A `SECRET_KEY` é gerada automaticamente uma única vez.

4. Entregue ao operador `docs/install_operator_steps.txt`, gerado pela preparação, e os exemplos [Nginx](docs/nginx.example.conf) e [systemd](docs/arcreports.service.example). O operador deve revisar hostname/TLS, atualizar configurações existentes sem duplicá-las e ativar o serviço com **um worker**.
5. Depois da ativação, execute `bash install.sh --verify`. Na v0.1.0 não existe `/health`: o script identifica o 404 e exige HTTP 200 em `/login`. Essa checagem não comprova login, banco, exportações ou ETL; valide esses fluxos pela interface. Use o hostname configurado no Nginx (ou IP se o operador configurar esse acesso).

O instalador cria o schema ORM em banco vazio antes do baseline. `alembic stamp head` sozinho **não cria tabelas**. Bancos existentes sem versão só recebem o stamp se o schema coincidir com o ORM e a revisão disponível for a baseline `74d249fd4a23`. Bancos versionados com migrações pendentes exigem aplicação revisada pelo operador; o script não usa stamp para pular migrações.

Para atualização: faça backup externo, solicite parada do serviço, disponibilize o código revisado preservando `.env`, `local.env`, `ldap.env`, `logs/`, `snapshots/` e `static/uploads/`; execute `bash install.sh` e repita a ativação/verificação. Sem `INSTALL_DB_CNF`, o provisionamento usa a conta administrativa do `.env`. Contas existentes mantêm as senhas; credencial divergente interrompe a preparação. A confirmação sobre `.env` permite apenas completar chaves ausentes, com cópia privada de backup; recusar mantém o arquivo intacto. Não há rollback automático de dependências ou DDL. Para migrações entre versões, siga o [workflow Alembic](docs/alembic_workflow.md).

Para instalar sem internet, use `PIP_NO_INDEX=1 PIP_FIND_LINKS=/caminho/wheels bash install.sh`, com todas as dependências compatíveis previamente disponíveis. Use `pip install -r requirements.txt`: o empacotamento via `pip install .` e o entry point do `pyproject.toml` não estão homologados.

### Configuração

Consulte [.env.example](.env.example), [local.env.example](local.env.example) e [ldap.env.example](ldap.env.example). Os exemplos não contêm segredos reais. O instalador usa `reports` e contas locais em `localhost:3306`; instalações com nomes/host/porta diferentes exigem revisão manual e são preservadas. `glpi_reports` é um nome histórico, não o default atual.

- `.env`: banco local, conta administrativa, gestão de contas, provisionamento opcional, chave, TTL, limite de linhas, console SQL e origem GLPI legada. Preferir conectores cadastrados na interface. `APP_SECRET_KEY` é apenas alias legado de fallback.
- `local.env`: administrador inicial. O startup cria/promove/reativa essa conta; não substitui um hash de senha já existente.
- `ldap.env`: bootstrap opcional com oito chaves LDAP. Após o cadastro, a configuração persistida no banco tem precedência nesse fluxo. Manter acesso local de contingência.

Não versione os arquivos preenchidos. Restrinja a leitura a `ia-dev` (`0600`), proteja também cópias `.bak` e preserve a chave junto dos backups. Não use `source .env`: os arquivos usam sintaxe python-dotenv. O serviço de exemplo deixa a aplicação carregá-los, sem `EnvironmentFile` duplicado. Variáveis herdadas do processo podem ter precedência sobre dotenv. No instalador, `${...}` é recusado para impedir interpolação de credenciais.

`SQL_CONSOLE_ENABLED=false` desativa o console no startup; administradores podem alterar a configuração persistida pela interface. Horários, retenções e aparência são configurações do portal no banco, não novas chaves `.env`.

### Arquitetura

```text
Origens MySQL/MariaDB ou PostgreSQL (credencial somente SELECT)
          │ adapters e ETL: carga full / incremental
          ▼
Réplicas MariaDB locais: glpi_local, redmine_local, zabbix_local,
                        bookstack_local, custom_local
          │ relatórios primários → relatórios derivados
          ▼
reports: metadados + resultados materializados + histórico + permissões
          │ FastAPI / SQLAlchemy / PyMySQL / Jinja2
          ▼
Nginx → UI Bootstrap + gráficos locais → PDF / Excel
          └─ APScheduler no processo web → jobs / snapshots SQL
```

Código em `app/`, interface em `templates/` e `static/`, auxiliares em `etl/`, migrações em `alembic/` e scripts históricos em `migrations/`. Origens PostgreSQL usam `psycopg2`; o destino continua MariaDB. As contas privilegiadas são exclusivas do servidor local.

### Usuários do banco

| Conta (`@localhost`) | Função | Grants do instalador |
|---|---|---|
| `glpi_portal` | Aplicação, resultados e schema local | `SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,INDEX,ALTER ON reports.*` |
| `portal_db_admin` | Criar databases e conceder privilégios das réplicas | `ALL PRIVILEGES ON *.* WITH GRANT OPTION` |
| `portal_db_user` | Gestão de contas de leitura | `CREATE USER ON *.*` + `SELECT ON reports.* WITH GRANT OPTION` |

São três contas MariaDB, distintas do usuário Linux `ia-dev` e do administrador do portal. O DBA de bootstrap é um pré-requisito de instalação. Grants são aditivos, sem revogar permissões antigas; acesso às réplicas depende do provisionamento dos conectores. Em origens GLPI e demais fontes, use contas separadas **somente SELECT**.

### Jobs automáticos

| Horário (America/Sao_Paulo) | Função |
|---|---|
| 02:00 diariamente | Criar snapshot SQL e aplicar retenção após sucesso |
| 03:00 diariamente | Limpar histórico/auditoria/autenticação antigos; retenção padrão 90 dias |
| 03:20 diariamente | Aplicar retenção de snapshots: padrão 10 arquivos e 20 GiB |
| 03:30 diariamente | Manutenção automática de índices |
| 06:30, 13:30, 20:30 por padrão | Relatórios globais agendados; configurável no portal |
| Horários e dias por conector | ETL full/incremental conforme flags e cadastro |

O scheduler usa fuso fixo `America/Sao_Paulo`; mudar o fuso de exibição não muda esses jobs. Horários salvos em instalações existentes podem diferir dos defaults. Relatórios podem aguardar ETL em andamento (padrão de espera: 30 minutos). Agendamentos rodam somente enquanto o processo web está ativo.

### O que não é suportado

- Oracle (previsto v0.3; ainda não implementado).
- SQL Server e SQLite como origem/destino de produção. SQLite é usado nos testes.
- Múltiplos workers sem coordenação.
- TLS nativo (configurar no Nginx).

### Segurança

Há hashing PBKDF2-HMAC-SHA256 para senhas locais, sessões assinadas com expiração, cookies HttpOnly/SameSite=Lax, proteção CSRF nas rotas que a verificam, limitação de tentativas de login por IP e auditoria. Credenciais de conectores/LDAP são cifradas com Fernet derivado da `SECRET_KEY`. Consultas de relatórios passam por validação de SELECT/WITH, identificadores e schemas permitidos; mantenha a restrição SELECT também na conta da origem.

Exportações individuais e em massa validam categoria antes de ler os resultados; negações são auditadas. Dashboards têm autorização própria. Esses mecanismos não equivalem a uma auditoria completa de todas as rotas. O cookie atual não define `Secure`; TLS depende do proxy. O exemplo Nginx substitui `X-Forwarded-For` pelo IP cliente para acesso direto; instalações com proxies adicionais exigem política de confiança revisada. Rate limit e locks são locais ao processo.

### Backup e recuperação

Snapshots SQL de `reports` ficam em `/opt/sites/glpi-portal/snapshots/`. A aplicação usa `mysqldump`, grava temporário, verifica o marcador `-- Dump completed` e publica o arquivo final por renomeação. Também é possível solicitar snapshot pela Saúde do Sistema. Os nomes usam UTC; o job usa horário de São Paulo.

A retenção por quantidade e tamanho pode remover até o snapshot mais recente; não significa dez dias garantidos. Mantenha cópias externas. Esses dumps não incluem automaticamente as réplicas, grants/contas, `.env`, `local.env`, `ldap.env`, uploads nem código: guarde-os separadamente. A opção `--routines --triggers` do dump pode exigir privilégios adicionais no MariaDB; os grants mínimos solicitados não incluem esses privilégios. Homologue o snapshot no servidor e ajuste a política com o DBA, sem ampliar grants silenciosamente.

Restauração é manual, em janela de manutenção, com serviço parado, backup atual e conta DBA local não root. Valide o dump primeiro em um banco de recuperação e confira versão do código/schema e `SECRET_KEY`. Exemplo apenas para o operador, após provisionar o banco de recuperação:

```bash
mysql --host=localhost --user=operador_restore --password reports_recuperacao < /caminho/privado/snapshot.sql
```

O dump contém comandos que substituem tabelas. Não execute em origem GLPI nem aponte o portal ao banco de recuperação sem revisar as restrições de schema. A promoção do restore para `reports` deve seguir procedimento do DBA. A restauração não foi automatizada nem homologada por este instalador.

### Desenvolvimento

Use o venv e as dependências de `requirements.txt`:

```bash
python -m compileall app/ -q
python -m pytest tests/ -q
bash -n install.sh
```

Execute testes em ambiente isolado, com credenciais fictícias e sem acesso aos bancos de produção: há um teste de integração que consulta `information_schema` se a conexão estiver disponível. A maioria usa SQLite em memória e mocks. Não inicie `app.main.startup` para validar documentação: ele altera o banco e inicia jobs.

Leia o [workflow Alembic](docs/alembic_workflow.md). Após mudanças no ORM, gere `alembic revision --autogenerate -m "descricao"`, revise o arquivo e teste em ambiente descartável; o operador aplica `alembic upgrade head` após snapshot. Nunca modifique revisões já aplicadas. A allowlist cobre 23 tabelas ORM; resultados e réplicas ficam fora. `alembic current`, `alembic history --verbose` e `alembic check` ajudam a verificar o estado; current/check precisam do banco configurado. Não use Alembic no GLPI.

### Licença

AGPL-3.0, conforme `pyproject.toml`. Assets de terceiros preservam seus avisos de licença em `static/vendor/`.

---

<a id="english"></a>
## English

### What it is

An agnostic data replication and analytics platform for BI. It connects to MySQL/MariaDB or PostgreSQL sources, replicates data locally, and supports reports and dashboards without an internet connection. Built for IT teams and the future open source community. The native interface language is Brazilian Portuguese; this README language selector only navigates document sections.

### Features

- Connectors: GLPI, Redmine, Zabbix, BookStack and Custom.
- Primary and derived reports with local materialization.
- Dashboards with KPI, Gauge, Bar, Pie and Table widgets.
- Time filters supporting ISO and Brazilian date formats.
- Local + LDAP authentication with local access as a fallback.
- Granular permissions by module and category.
- Administrative SQL console.
- Automatic daily snapshots.
- Browser-based dashboard PDF export and report Excel export.
- 100% offline operation — no CDN dependencies in templates; assets in `static/vendor/`.

Offline means no external internet during operation. Replication requires network access to sources; LDAP requires access to the directory. Dependency installation requires internet or a previously prepared local wheel repository. Each connector depends on the source schema and permissions; this does not imply automatic compatibility with every application.

### Minimum requirements

| Component | Minimum version | Tested on |
|---|---|---|
| RHEL / Rocky / AlmaLinux | 9.x | RHEL 9.8 |
| Fedora | 38+ | — |
| Ubuntu | 22.04 LTS+ | — |
| Debian | 11+ | — |
| Python | 3.9 | 3.9.25 |
| MariaDB | 10.5 | 10.5.29 |
| Nginx | 1.18 | 1.20.1 |

“Tested on” identifies the environment inventoried on September 10, 2026; clean installation and Rocky/AlmaLinux still require qualification. Size RAM and disk for replicas, results, indexes and snapshots. `mysql`, `mysqldump`, `curl`, `venv` support and dependencies from [requirements.txt](requirements.txt) are required. Local MariaDB is mandatory even with PostgreSQL sources.

### Quick installation

Installation is hybrid: the operator prepares infrastructure and activates the service; [install.sh](install.sh) automates application preparation and local MariaDB provisioning. The script must be run as root. It automatically creates the `ia-dev` user—the owner of the project, files, virtual environment and systemd service—without a password for direct login. For manual access, use `sudo su - ia-dev`. Application operations (virtual environment, `.env`, Alembic) run internally as `ia-dev` through `runuser`. The script does not configure the firewall or SELinux automatically, but displays instructions at the end when it detects an active firewalld or ufw service.

1. Run the installer as root on a server with RHEL 9.x, Rocky, AlmaLinux, Fedora 38+, Ubuntu 22.04+ or Debian 11+. The script checks and automatically installs Python 3.9+, MariaDB 10.5+ and Nginx 1.18+ through `dnf` or `apt-get` when necessary, creates the `ia-dev` user (without a password for direct login), and prepares `/opt/sites/glpi-portal`.
2. Prepare a **non-root** DBA credential already authorized on local MariaDB in a private file owned by `ia-dev`, mode `0600`. Example contents (INI format; literal password without quotes):

   ```ini
   [client]
   host=localhost
   port=3306
   user=dba_instalacao
   password=YOUR_STRONG_PASSWORD
   ```

3. As root, clone and run:

   ```bash
   git clone https://github.com/joancalazans/arcreports.git \
     /opt/sites/glpi-portal
   cd /opt/sites/glpi-portal
   sudo bash install.sh
   ```

   Or with a custom DBA credential:

   ```bash
   sudo INSTALL_DB_CNF=/path/to/bootstrap.cnf \
     bash install.sh
   ```

   Enter only the three database passwords, `ADMIN_USERNAME` and `ADMIN_PASSWORD` when prompted. `SECRET_KEY` is generated automatically only once.

4. Give the operator `docs/install_operator_steps.txt`, generated during preparation, and the [Nginx](docs/nginx.example.conf) and [systemd](docs/arcreports.service.example) examples. The operator must review hostname/TLS, update existing configurations without duplication, and activate the service with **one worker**.
5. After activation, run `bash install.sh --verify`. Version 0.1.0 has no `/health`: the script detects 404 and requires HTTP 200 from `/login`. This does not verify authentication, database access, exports or ETL; validate those through the UI. Use the configured Nginx hostname (or IP if the operator enables that access).

The installer creates the ORM schema in an empty database before the baseline. `alembic stamp head` alone **does not create tables**. Existing unversioned databases are stamped only if their schema matches the ORM and the available revision is baseline `74d249fd4a23`. Versioned databases with pending migrations require reviewed application by the operator; the script never uses stamp to skip migrations.

For upgrades: make an external backup, request service shutdown, place the reviewed code while preserving `.env`, `local.env`, `ldap.env`, `logs/`, `snapshots/` and `static/uploads/`; run `bash install.sh` and repeat activation/verification. Without `INSTALL_DB_CNF`, provisioning uses the administrative account from `.env`. Existing accounts keep their passwords; mismatched credentials stop preparation. The `.env` confirmation only fills missing keys and creates a private backup; declining leaves the file intact. There is no automatic dependency or DDL rollback. For migrations between versions, follow the [Alembic workflow](docs/alembic_workflow.md).

For installation without internet, use `PIP_NO_INDEX=1 PIP_FIND_LINKS=/path/to/wheels bash install.sh` with all compatible dependencies already available. Use `pip install -r requirements.txt`: packaging through `pip install .` and the `pyproject.toml` entry point have not been qualified.

### Configuration

See [.env.example](.env.example), [local.env.example](local.env.example) and [ldap.env.example](ldap.env.example). Examples contain no real secrets. The installer uses `reports` and local accounts at `localhost:3306`; installations with different names/host/port require manual review and are preserved. `glpi_reports` is a historical name, not the current default.

- `.env`: local database, administrative account, account management, optional provisioning, secret key, TTL, row limit, SQL console and legacy GLPI source. Prefer connectors configured through the UI. `APP_SECRET_KEY` is only a legacy fallback alias.
- `local.env`: initial administrator. Startup creates/promotes/reactivates this account; it does not replace an existing password hash.
- `ldap.env`: optional bootstrap with eight LDAP keys. Once configured, database-persisted settings take precedence in this flow. Keep local fallback access.

Never commit populated files. Restrict reading to `ia-dev` (`0600`), also protect `.bak` copies, and preserve the key with backups. Do not use `source .env`: files use python-dotenv syntax. The example service lets the application load them without a duplicate `EnvironmentFile`. Inherited process variables can override dotenv values. The installer rejects `${...}` to prevent credential interpolation.

`SQL_CONSOLE_ENABLED=false` disables the console at startup; administrators can change the persisted setting through the UI. Schedules, retention and appearance are portal database settings, not extra `.env` keys.

### Architecture

```text
MySQL/MariaDB or PostgreSQL sources (SELECT-only credential)
          │ adapters and ETL: full / incremental load
          ▼
Local MariaDB replicas: glpi_local, redmine_local, zabbix_local,
                        bookstack_local, custom_local
          │ primary reports → derived reports
          ▼
reports: metadata + materialized results + history + permissions
          │ FastAPI / SQLAlchemy / PyMySQL / Jinja2
          ▼
Nginx → Bootstrap UI + local charts → PDF / Excel
          └─ APScheduler inside web process → jobs / SQL snapshots
```

Code lives in `app/`, UI in `templates/` and `static/`, helpers in `etl/`, migrations in `alembic/` and historical scripts in `migrations/`. PostgreSQL sources use `psycopg2`; the destination remains MariaDB. Privileged accounts belong exclusively to the local server.

### Database users

| Account (`@localhost`) | Role | Installer grants |
|---|---|---|
| `glpi_portal` | Application, results and local schema | `SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,INDEX,ALTER ON reports.*` |
| `portal_db_admin` | Create databases and grant replica privileges | `ALL PRIVILEGES ON *.* WITH GRANT OPTION` |
| `portal_db_user` | Manage read-only accounts | `CREATE USER ON *.*` + `SELECT ON reports.* WITH GRANT OPTION` |

These are three MariaDB accounts, distinct from Linux user `ia-dev` and the portal administrator. A bootstrap DBA is an installation prerequisite. Grants are additive, without revoking existing permissions; replica access depends on connector provisioning. Use separate **SELECT-only** accounts on GLPI and all other sources.

### Automatic jobs

| Time (America/Sao_Paulo) | Function |
|---|---|
| Daily at 02:00 | Create SQL snapshot and apply retention after success |
| Daily at 03:00 | Remove old history/audit/authentication records; default retention 90 days |
| Daily at 03:20 | Apply snapshot retention: default 10 files and 20 GiB |
| Daily at 03:30 | Automatic index maintenance |
| 06:30, 13:30, 20:30 by default | Globally scheduled reports; configurable in portal |
| Per-connector times and days | Full/incremental ETL according to flags and configuration |

The scheduler uses fixed `America/Sao_Paulo` time; changing the display timezone does not change these jobs. Existing installations may have different saved schedules. Reports can wait for running ETL (default wait: 30 minutes). Schedules run only while the web process is active.

### Unsupported features

- Oracle (planned for v0.3; not implemented yet).
- SQL Server and SQLite as production sources/destinations. SQLite is used in tests.
- Multiple workers without coordination.
- Native TLS (configure it in Nginx).

### Security

The application uses PBKDF2-HMAC-SHA256 password hashing, signed expiring sessions, HttpOnly/SameSite=Lax cookies, CSRF protection on routes that check it, IP-based login attempt limiting and audit logs. Connector/LDAP credentials use Fernet encryption derived from `SECRET_KEY`. Report queries undergo SELECT/WITH, identifier and allowed-schema validation; enforce SELECT-only privileges on source accounts too.

Individual and bulk exports validate categories before reading results; denials are audited. Dashboards have their own authorization. These mechanisms do not constitute a complete audit of all routes. The current cookie does not set `Secure`; TLS relies on the proxy. The Nginx example replaces `X-Forwarded-For` with the client IP for direct access; installations with additional proxies need a reviewed trust policy. Rate limiting and locks are process-local.

### Backup and recovery

SQL snapshots of `reports` are stored in `/opt/sites/glpi-portal/snapshots/`. The application uses `mysqldump`, writes a temporary file, checks the `-- Dump completed` marker and publishes the final file by renaming. Manual snapshots are also available through System Health. Filenames use UTC; the job uses São Paulo time.

Count and size retention may remove even the newest snapshot; it does not guarantee ten days. Keep external copies. Dumps do not automatically include replicas, grants/accounts, `.env`, `local.env`, `ldap.env`, uploads or code: back those up separately. Dump options `--routines --triggers` may require additional MariaDB privileges; the requested minimum grants do not include them. Qualify snapshot creation on the server and review policy with the DBA without silently expanding grants.

Restoration is manual, during maintenance with the service stopped, a current backup and a local non-root DBA account. Validate the dump first in a recovery database and check code/schema version and `SECRET_KEY`. Operator-only example, after provisioning the recovery database:

```bash
mysql --host=localhost --user=operador_restore --password reports_recuperacao < /private/path/snapshot.sql
```

The dump includes commands that replace tables. Never run it on the GLPI source or point the portal to the recovery database without reviewing schema restrictions. Promoting the restored data to `reports` must follow the DBA procedure. Restoration has not been automated or qualified by this installer.

### Development

Use the venv and dependencies from `requirements.txt`:

```bash
python -m compileall app/ -q
python -m pytest tests/ -q
bash -n install.sh
```

Run tests in an isolated environment with dummy credentials and no access to production databases: an integration test queries `information_schema` if a connection is available. Most tests use in-memory SQLite and mocks. Do not invoke `app.main.startup` to validate documentation: it changes the database and starts jobs.

Read the [Alembic workflow](docs/alembic_workflow.md). After ORM changes, generate `alembic revision --autogenerate -m "description"`, review it and test in a disposable environment; the operator applies `alembic upgrade head` after a snapshot. Never modify applied revisions. The allowlist covers 23 ORM tables; results and replicas are excluded. `alembic current`, `alembic history --verbose` and `alembic check` help inspect state; current/check require the configured database. Never use Alembic on GLPI.

### License

AGPL-3.0, as declared in `pyproject.toml`. Third-party assets retain their license notices in `static/vendor/`.
