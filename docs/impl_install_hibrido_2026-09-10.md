# Install.sh híbrido — ArcReports

Data: 2026-09-10

## O que foi implementado

- Execução obrigatória como root, mantendo as operações de aplicação sob o usuário `ia-dev` por meio de `runuser`.
- Detecção de Python, MariaDB, Nginx, Git, curl e mysqldump, com validação das versões mínimas aplicáveis.
- Instalação automática por `dnf` quando o componente está ausente ou abaixo da versão mínima, seguida de nova validação.
- Ativação de MariaDB e Nginx por systemd. Em instalação nova do MariaDB, a rotina não interativa equivalente ao `mysql_secure_installation` remove contas anônimas, login root remoto e o schema de teste. Essa rotina nunca acessa o banco GLPI.
- Criação idempotente do usuário de serviço e do diretório de instalação.
- Criação/reutilização do venv como `ia-dev`, instalação de `requirements.txt`, `pip check` e suporte a pacotes offline com `PIP_FIND_LINKS`/`PIP_NO_INDEX`.
- Geração de `.env` com senhas solicitadas sem eco e `SECRET_KEY` aleatória. Uma chave existente nunca é substituída; no modo update, o arquivo inteiro é preservado.
- Criação de `local.env` com credenciais administrativas e preservação de `local.env`/`ldap.env` existentes.
- Provisionamento idempotente e exclusivo do MariaDB local: banco `reports`, usuários `glpi_portal`, `portal_db_admin` e `portal_db_user`, além dos grants correspondentes.
- Baseline Alembic somente quando o banco `reports` foi criado pela execução atual.
- Instalação da configuração Nginx quando ausente. Uma configuração existente é preservada automaticamente no update e só é substituída, com backup, após confirmação em instalação nova.
- Instalação da unidade systemd quando ausente, seguida de habilitação/inicialização do ArcReports.
- Verificação final de `http://127.0.0.1:8000/login`, exigindo HTTP 200, e mensagens de erro com linha, comando e orientação operacional.

Nenhum arquivo em `app/` ou outro código da aplicação foi alterado.

## Fluxo de instalação

1. Confirma execução como root e seleciona automaticamente nova instalação quando não há artefatos anteriores.
2. Verifica cada pré-requisito; instala via `dnf` e valida novamente quando necessário.
3. Cria `ia-dev` e `/opt/sites/glpi-portal` quando ausentes.
4. Prepara venv, `.env`, `local.env`, `ldap.env` e diretórios persistentes como `ia-dev`.
5. Solicita a credencial root do MariaDB local, cria banco/contas apenas se ausentes e aplica grants idempotentes.
6. Registra `alembic stamp head` apenas para banco criado nessa instalação.
7. Instala/valida Nginx, instala/habilita a unidade systemd e inicia o ArcReports.
8. Aguarda a aplicação e confirma resposta HTTP 200 em `/login`.

## Fluxo de atualização

Quando `.env`, `venv` ou a unidade `arcreports.service` já existem, o operador escolhe entre nova instalação e update. Também é possível definir `ARCREPORTS_INSTALL_MODE=update` para selecionar o modo explicitamente.

No update, o instalador:

- preserva integralmente `.env`, incluindo `SECRET_KEY`;
- preserva `local.env`, `ldap.env`, `logs/`, `snapshots/`, `uploads/` e `static/uploads/`;
- reutiliza o venv e atualiza somente as dependências declaradas;
- não executa `alembic stamp head` no banco existente;
- mantém configurações Nginx e systemd existentes;
- reaplica apenas operações idempotentes de banco e reinicia o serviço para carregar a versão atual.

## Resultado

- `bash -n install.sh`: ok
- Instalador: não executado, conforme solicitado.
