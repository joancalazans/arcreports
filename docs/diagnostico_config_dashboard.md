# Diagnóstico Completo: Config Dashboard

Escopo analisado: `app/routes/dashboard.py`, `templates/admin_dashboard_config.html`, `app/models.py`, `app/deletion_dependencies.py`, `app/main.py`, `templates/base.html` e `templates/dashboard.html`. Não foram executados SQL nem comandos destrutivos.

## 1. Backend - `app/routes/dashboard.py`

### 1a. Endpoints relacionados

| Finalidade | Método / URL | Parâmetros | Retorno |
|---|---|---|---|
| Listagem/configuração de dashboards e widgets | `GET /admin/dashboard-config` (`app/routes/dashboard.py:1752`) | Query `dashboard_id` opcional (`app/routes/dashboard.py:1760`) | HTML `admin_dashboard_config.html` via `dashboard_widget_context` (`app/routes/dashboard.py:1636`, `app/routes/dashboard.py:1664`) |
| Criação de dashboard | `POST /admin/dashboard-config/dashboards` (`app/routes/dashboard.py:1765`) | Form: `name`, `description`, `is_active`, `sort_order`, `category_ids` (`app/routes/dashboard.py:1770-1779`) | Redirect para `/admin/dashboard-config?dashboard_id=...` (`app/routes/dashboard.py:1784`) |
| Edição de dashboard | `POST /admin/dashboard-config/dashboards/{dashboard_id}` (`app/routes/dashboard.py:1787`) | Path `dashboard_id`; form: `name`, `description`, `is_active`, `sort_order`, `category_ids` (`app/routes/dashboard.py:1792-1803`) | Redirect para dashboard atual (`app/routes/dashboard.py:1806`) |
| Duplicação de dashboard | `POST /admin/dashboard-config/dashboards/{dashboard_id}/duplicate` (`app/routes/dashboard.py:1809`) | Path `dashboard_id` | Cria dashboard e copia widgets, incluindo `source_id_b`, `meta_gauge`, `cor_secundaria` (`app/routes/dashboard.py:1831-1853`); redirect (`app/routes/dashboard.py:1855`) |
| Dependências antes de excluir dashboard | `GET /admin/dashboard-config/dashboards/{dashboard_id}/dependencies` (`app/routes/dashboard.py:1858`) | Path `dashboard_id` | JSON `{success, dashboard, widget_count, widgets, has_dependencies}` (`app/routes/dashboard.py:1863-1866`; payload em `app/deletion_dependencies.py:112-131`) |
| Exclusão de dashboard | `POST /admin/dashboard-config/dashboards/{dashboard_id}/delete` (`app/routes/dashboard.py:1869`) | Path `dashboard_id`; query/header `cascade` opcional (`app/routes/dashboard.py:1881`) | Redirect; bloqueia se for o único ativo (`app/routes/dashboard.py:1877-1880`) ou se houver widgets sem cascata (`app/routes/dashboard.py:1882-1885`) |
| Listagem de widgets por dashboard | Indireto em `GET /admin/dashboard-config` | Query `dashboard_id` | `dashboard_widget_context` consulta `DashboardWidget.dashboard_id == current_dashboard.id` e passa `widgets` ao template (`app/routes/dashboard.py:1651-1674`) |
| Criação de widget | `POST /admin/dashboard-config/widgets` (`app/routes/dashboard.py:1897`) | Form: `dashboard_id`, `source_id`, `source_id_b`, `title`, `widget_type`, `label_field`, `value_field`, `series_field`, `time_field`, `aggregation`, `top_n`, `color`, `icone`, `comparar_com`, `meta_gauge`, `cor_secundaria`, `sort_order`, `size` (`app/routes/dashboard.py:1902-1916`, `app/routes/dashboard.py:1707-1749`) | AJAX: JSON `{success: true, widget: ...}` (`app/routes/dashboard.py:1921-1923`); não AJAX: redirect (`app/routes/dashboard.py:1924`) |
| Edição de widget | `POST /admin/dashboard-config/widgets/{widget_id}` (`app/routes/dashboard.py:1927`) | Path `widget_id`; mesmos campos do widget (`app/routes/dashboard.py:1932-1949`) | AJAX: JSON `{success: true, widget: ...}` (`app/routes/dashboard.py:1953-1955`); não AJAX: redirect (`app/routes/dashboard.py:1956`) |
| Exclusão de widget | `POST /admin/dashboard-config/widgets/{widget_id}/delete` (`app/routes/dashboard.py:1959`) | Path `widget_id`; CSRF | AJAX: JSON `{success: true, message}` (`app/routes/dashboard.py:1972-1973`); não AJAX: redirect (`app/routes/dashboard.py:1974`) |
| Bulk delete AJAX de widgets | `POST /dashboard/widgets/bulk-delete` (`app/routes/dashboard.py:1977`) | JSON `{widget_ids: [...]}` (`app/routes/dashboard.py:1982-1997`) | JSON `{deleted, errors}` (`app/routes/dashboard.py:2016`) |
| Ações em massa por form | `POST /admin/dashboard-config/widgets/bulk` (`app/routes/dashboard.py:2019`) | Form: `bulk_action`, `bulk_confirmation`, `widget_ids`, `dashboard_id` (`app/routes/dashboard.py:2024-2028`) | Redirect; suporta `activate`, `deactivate`, `delete`, `delete_all` (`app/routes/dashboard.py:2030-2078`) |
| Listagem de fontes | `GET /admin/dashboard-sources` (`app/routes/dashboard.py:1444`) | Nenhum | HTML `admin_dashboard_sources.html`, com fontes, tabelas e colunas (`app/routes/dashboard.py:1449-1480`) |
| Pesquisa/listagem AJAX de fontes | `GET /admin/dashboard-sources/search` (`app/routes/dashboard.py:1483`) | Query `q` opcional (`app/routes/dashboard.py:1484-1493`) | JSON lista de `dashboard_source_payload` (`app/routes/dashboard.py:1495-1496`) |
| Salvar fonte | `POST /admin/dashboard-sources/{table_name}` (`app/routes/dashboard.py:1499`) | Path `table_name`; form `name`, `description`, `is_active`, `default_time_field`, `category`, `note` (`app/routes/dashboard.py:1515-1530`) | AJAX: JSON `{success, source_id}` (`app/routes/dashboard.py:1532-1533`); não AJAX: redirect (`app/routes/dashboard.py:1534`) |
| Habilitar/desabilitar fonte | `POST /admin/dashboard-sources/{source_id}/enable` / `disable` (`app/routes/dashboard.py:1537`, `app/routes/dashboard.py:1567`) | Path `source_id` | JSON com status (`app/routes/dashboard.py:1557-1564`, `app/routes/dashboard.py:1586-1587`) |
| Dependências/exclusão de fonte | `GET /admin/dashboard-sources/{source_id}/dependencies`, `POST /admin/dashboard-sources/{source_id}/delete` (`app/routes/dashboard.py:1590`, `app/routes/dashboard.py:1601`) | Path `source_id`; header cascata na exclusão (`app/routes/dashboard.py:1606-1613`) | JSON; exclusão remove widgets dependentes se cascata confirmada (`app/routes/dashboard.py:1620-1633`) |

Não existe endpoint específico em `app/routes/dashboard.py` para "carregar colunas de uma fonte" a partir do Config Dashboard. As colunas são pré-carregadas no HTML.

### 1b. Como as colunas de uma fonte são carregadas

- A função central é `dashboard_table_columns(table_name)` (`app/routes/dashboard.py:176`).
- Antes de consultar colunas, ela valida se a tabela existe/é permitida com `dashboard_source_table_exists` (`app/routes/dashboard.py:177`, `app/routes/dashboard.py:156-173`).
- A query de colunas é em `information_schema.COLUMNS`: `SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name ORDER BY ORDINAL_POSITION` (`app/routes/dashboard.py:181-188`).
- Filtros aplicados: nome da coluna deve casar `IDENTIFIER_RE` e não pode ser coluna técnica (`app/routes/dashboard.py:190-194`). Coluna técnica é a que começa com `__` ou está em `hidden_dashboard_columns()` (`app/routes/dashboard.py:573-584`).
- Não há deduplicação explícita nessa função; ela retorna uma lista por tabela na ordem de `ORDINAL_POSITION` (`app/routes/dashboard.py:190-194`).
- Para o Config Dashboard, `source_columns_map(sources)` monta `{source.id: colunas}` (`app/routes/dashboard.py:211-212`) e é passado ao template como `source_columns` (`app/routes/dashboard.py:1672-1674`). Portanto retorna HTML renderizado, não JSON.
- Em `GET /admin/dashboard-sources`, as colunas também são embutidas no HTML/payload de fontes (`app/routes/dashboard.py:1452-1464`); em `enable`, o JSON inclui `columns` quando `include_columns=True` (`app/routes/dashboard.py:1557-1564`, `app/routes/dashboard.py:264-266`).

### 1c. Como widget é salvo

O create (`app/routes/dashboard.py:1897-1924`) e update (`app/routes/dashboard.py:1927-1956`) coletam form via `form_data(request)` (`app/routes/common.py:12-15`), validam dashboard e fontes, e chamam `apply_dashboard_widget` (`app/routes/dashboard.py:1915-1918`, `app/routes/dashboard.py:1947-1951`).

Campos persistidos em `apply_dashboard_widget`:

- `source` e `source_b` (`app/routes/dashboard.py:1725-1726`)
- `title`, `widget_type`, `label_field`, `value_field`, `series_field`, `time_field` (`app/routes/dashboard.py:1727-1732`)
- `aggregation`, `top_n`, `color`, `icone`, `comparar_com`, `meta_gauge`, `cor_secundaria`, `sort_order`, `size` (`app/routes/dashboard.py:1733-1749`)
- `dashboard` é definido no create pelo construtor (`app/routes/dashboard.py:1914`) e no update por `widget.dashboard = current_dashboard` (`app/routes/dashboard.py:1948-1949`).

`source_id_b` é salvo indiretamente por `widget.source_b = source_b` apenas para `gauge` e `comparativo` (`app/routes/dashboard.py:1726`). `cor_secundaria` é salva para `gauge` e `comparativo`, com fallback `"red"` se inválida ou ausente (`app/routes/dashboard.py:1742-1746`).

Validações existentes:

- Dashboard precisa existir (`app/routes/dashboard.py:1903-1906`, `app/routes/dashboard.py:1936-1939`).
- Fonte A precisa existir, estar ativa e ter tabela local existente (`app/routes/dashboard.py:1907-1909`, `app/routes/dashboard.py:1940-1942`).
- Fonte B, se enviada, precisa existir, estar ativa e ter tabela local existente (`app/routes/dashboard.py:1910-1913`, `app/routes/dashboard.py:1943-1946`).
- Campos `label/value/series/time`, se enviados, precisam existir nas colunas da Fonte A (`app/routes/dashboard.py:1707-1718`).
- Agregação precisa estar em `DASHBOARD_AGGREGATIONS` (`app/routes/dashboard.py:1719-1721`).
- Para agregação diferente de `count`, `value_field` é obrigatório (`app/routes/dashboard.py:1722-1723`).
- Não há validação obrigando `source_id_b` para `gauge` ou `comparativo`.
- Não há validação obrigando `label_field` para `pizza/barra/linha/area/tabela`; sem label, agrupa nada e retorna agregação global (`app/routes/dashboard.py:536-559`).

Problema técnico importante: `form_data()` usa `parse_qs` sobre o corpo bruto (`app/routes/common.py:12-15`). Isso só é compatível com `application/x-www-form-urlencoded`. O JS de criação/edição envia `new FormData(form)` (`templates/admin_dashboard_config.html:832-839`, `templates/admin_dashboard_config.html:952-959`), o que normalmente gera `multipart/form-data`; nesse caso o backend pode não receber os campos corretamente.

### 1d. Gauge/comparativo duplicado

Não há validação de unicidade para widget `gauge` ou `comparativo` em `DashboardWidget` (`app/models.py:248-287`), nem em `apply_dashboard_widget` (`app/routes/dashboard.py:1701-1749`), nem nas rotas create/update (`app/routes/dashboard.py:1897-1956`). A busca por constraints no código só encontra `unique=True` em outras entidades e em `DashboardSource.source_table` (`app/models.py:49`, `app/models.py:83`, `app/models.py:96`, `app/models.py:231`).

Se ocorrer erro ao salvar via AJAX, o caminho esperado seria JSON com `success: false`, mas `dashboard_widget_context()` sempre retorna HTML (`app/routes/dashboard.py:1636-1683`). Como o frontend faz `response.json()` sem `catch` (`templates/admin_dashboard_config.html:840-844`, `templates/admin_dashboard_config.html:960-964`), um retorno HTML vira erro de parse e não é exibido ao usuário.

## 2. Frontend - `templates/admin_dashboard_config.html`

### 2a. Estrutura geral

- Abas: renderizadas por loop em `dashboards`, cada aba é um `<a>` com `href="/admin/dashboard-config?dashboard_id={{ dash.id }}"`, `data-dashboard-tab` e `data-dashboard-id` (`templates/admin_dashboard_config.html:188-197`). A aba ativa é definida pelo servidor com `current_dashboard.id` (`templates/admin_dashboard_config.html:191`).
- Novo dashboard: collapse `#newDashboardForm` e form `POST /admin/dashboard-config/dashboards` (`templates/admin_dashboard_config.html:198-224`).
- Edição/exclusão/duplicação do dashboard atual: botões/forms no cabeçalho (`templates/admin_dashboard_config.html:243-252`) e modal de edição para cada dashboard (`templates/admin_dashboard_config.html:257-305`).
- Formulário de novo widget: `POST /admin/dashboard-config/widgets`, marcado com `data-widget-form data-new-widget-form` (`templates/admin_dashboard_config.html:308-416`).
- Formulário de edição inline: cada widget é um `<form class="dashboard-widget-row" ... data-widget-form>` com corpo oculto `.widget-edit-body` (`templates/admin_dashboard_config.html:434-552`). O delete fica em form separado (`templates/admin_dashboard_config.html:553-555`).
- HTML da Fonte B novo widget: `#field-source-b-new`, `name="source_id_b"`, inicialmente `style="display:none"` (`templates/admin_dashboard_config.html:330-339`).
- HTML da Fonte B edição: `#field-source-b-{{ widget.id }}`, `name="source_id_b"`, inicialmente `style="display:none"` (`templates/admin_dashboard_config.html:465-473`).
- Campos `label/value/series/time`: macro `field_select` (`templates/admin_dashboard_config.html:2-14`); no novo widget em `templates/admin_dashboard_config.html:355-359`; na edição inline em `templates/admin_dashboard_config.html:489-493`.

### 2b. Funções JavaScript

Funções presentes no template:

- `getActiveDashboardId()` retorna dashboard ativo pela aba `.tab.active` ou `currentDashboardId` (`templates/admin_dashboard_config.html:589-591`).
- `storeActiveDashboardTab(dashboardId)` grava aba ativa em `sessionStorage` (`templates/admin_dashboard_config.html:593-597`).
- `reactivateDashboardTab(dashboardId)` tenta restaurar aba após AJAX; se o ID difere do atual, chama `tab.click()` (`templates/admin_dashboard_config.html:599-605`).
- Bloco de restauração inicial redireciona para `?dashboard_id=` se há aba salva e a URL não tem `dashboard_id` (`templates/admin_dashboard_config.html:607-613`).
- Listener das abas grava o ID no clique (`templates/admin_dashboard_config.html:614-616`).
- `escapeHtml(value)` escapa texto para HTML (`templates/admin_dashboard_config.html:618-622`).
- `restoreScroll(savedScrollY)` restaura rolagem (`templates/admin_dashboard_config.html:624-626`).
- `showDashboardToast(message)` cria/exibe toast (`templates/admin_dashboard_config.html:628-640`).
- `widgetCards()` lista cards de widgets (`templates/admin_dashboard_config.html:642-644`).
- `updateWidgetCount()` atualiza contador (`templates/admin_dashboard_config.html:646-648`).
- `reorderWidgetGrid()` reordena cards pelo `data-order` (`templates/admin_dashboard_config.html:650-655`).
- `removeWidgetCard(widgetId)` remove card e form de delete, atualiza contador e estado bulk (`templates/admin_dashboard_config.html:657-674`).
- `closeWidgetEditor(card, reset)` fecha edição inline, reseta form e reordena (`templates/admin_dashboard_config.html:676-685`).
- `openWidgetEditor(widgetId)` inicializa o form do widget, abre edição inline e move o card para o topo (`templates/admin_dashboard_config.html:687-698`).
- `submitDeleteForm(form, cascade)` submete exclusão de dashboard com `cascade=1` se necessário (`templates/admin_dashboard_config.html:715-718`).
- Handler de delete de dashboard consulta dependências (`templates/admin_dashboard_config.html:720-743`).
- Handler de delete de widget abre modal de confirmação (`templates/admin_dashboard_config.html:745-754`).
- Handler do botão `deleteConfirm` executa exclusão de dashboard, bulk widgets ou widget individual (`templates/admin_dashboard_config.html:756-825`).
- Handler do novo widget salva via `fetch(..., body: new FormData(form))` e cria um card simplificado (`templates/admin_dashboard_config.html:827-875`).
- `normalizeWidgetType(value)` normaliza tipo (`templates/admin_dashboard_config.html:877-879`).
- `dashboardFieldOptions(field)` extrai e cacheia opções de colunas por `data-source-id`; deduplica apenas dentro da mesma fonte por `value` (`templates/admin_dashboard_config.html:881-900`).
- `syncDashboardSourceFields(form)` filtra selects `.dashboard-field-select` pela fonte escolhida (`templates/admin_dashboard_config.html:902-915`).
- `syncDashboardTypeFields(form)` controla visibilidade por tipo; Fonte B é exibida quando tipo é `gauge` ou `comparativo` (`templates/admin_dashboard_config.html:917-935`).
- `initDashboardWidgetForms(root)` inicializa forms, sincroniza fonte/tipo e registra submit AJAX para edição inline (`templates/admin_dashboard_config.html:937-979`).
- Listener global `change` chama `syncDashboardSourceFields` para fonte e `syncDashboardTypeFields` para tipo (`templates/admin_dashboard_config.html:981-989`).
- `MutationObserver` inicializa forms adicionados depois (`templates/admin_dashboard_config.html:992-1004`).
- `syncBulkState()` atualiza checkboxes, contador e botão Aplicar (`templates/admin_dashboard_config.html:1015-1031`).
- `initWidgetBulkActions()` registra listeners de selecionar todos, mudança de checkbox e botão Aplicar (`templates/admin_dashboard_config.html:1033-1076`).

Onde os listeners deveriam ser registrados:

- Editar/cancelar: logo após as funções de editor (`templates/admin_dashboard_config.html:700-706`).
- Delete dashboard/widget: `templates/admin_dashboard_config.html:720-754`.
- Confirmar delete/bulk: `templates/admin_dashboard_config.html:756-825`.
- Novo widget: `templates/admin_dashboard_config.html:827-875`.
- Fonte/tipo: listener global em `templates/admin_dashboard_config.html:981-989`.
- Inicialização forms: `templates/admin_dashboard_config.html:991`.
- Bulk actions: `DOMContentLoaded` ou imediato em `templates/admin_dashboard_config.html:1078-1082`.

Problema estrutural: o script inline do template está dentro do bloco `content` (`templates/admin_dashboard_config.html:579-1086`), enquanto `bootstrap.bundle.min.js` só é carregado depois do conteúdo em `base.html:108`. Assim, ao chegar em `new bootstrap.Modal(...)` (`templates/admin_dashboard_config.html:709`), `bootstrap` ainda não existe e a execução do script para. Tudo abaixo da linha 709 não é registrado.

### 2c. Fluxo ao selecionar tipo `gauge`

Fluxo esperado:

1. Usuário muda `select.dashboard-type-select` (`templates/admin_dashboard_config.html:346-352`).
2. Listener global de `change` captura o evento (`templates/admin_dashboard_config.html:981-989`).
3. `syncDashboardTypeFields(form)` normaliza o tipo (`templates/admin_dashboard_config.html:917-921`).
4. Para `gauge`, oculta `label_field`, `series_field`, `top_n` (`templates/admin_dashboard_config.html:922`, `templates/admin_dashboard_config.html:927`), mostra `meta_gauge` (`templates/admin_dashboard_config.html:925`, `templates/admin_dashboard_config.html:930`) e mostra Fonte B/cor secundária (`templates/admin_dashboard_config.html:926`, `templates/admin_dashboard_config.html:931-934`).

O que acontece de fato:

- O script quebra antes da definição e registro de `syncDashboardTypeFields`, no ponto `new bootstrap.Modal(...)` (`templates/admin_dashboard_config.html:709`) porque Bootstrap só é carregado depois (`templates/base.html:108`).
- O listener global de mudança nunca é registrado (`templates/admin_dashboard_config.html:981-989`).
- Resultado: Fonte B permanece com `style="display:none"` (`templates/admin_dashboard_config.html:330`, `templates/admin_dashboard_config.html:465`).

Linha exata do rompimento: `templates/admin_dashboard_config.html:709`, combinada com a ordem de carregamento em `templates/base.html:105-108`.

### 2d. Fluxo ao selecionar uma fonte

Fluxo esperado:

1. O template renderiza todos os campos com opções de todas as fontes, cada `<option>` com `data-source-id` (`templates/admin_dashboard_config.html:2-14`).
2. Na inicialização, `initDashboardWidgetForms()` chama `syncDashboardSourceFields(form)` (`templates/admin_dashboard_config.html:937-946`).
3. `dashboardFieldOptions()` cacheia opções por fonte e deduplica valores repetidos dentro da mesma fonte (`templates/admin_dashboard_config.html:881-900`).
4. `syncDashboardSourceFields()` substitui as opções por apenas as colunas da fonte selecionada (`templates/admin_dashboard_config.html:902-915`).
5. Ao trocar a fonte, o listener global chama novamente `syncDashboardSourceFields()` (`templates/admin_dashboard_config.html:981-988`).

O que acontece de fato:

- Como o script quebra em `templates/admin_dashboard_config.html:709`, a inicialização da linha `991` nunca roda.
- Os selects ficam com o HTML bruto da macro, que contém colunas de todas as fontes (`templates/admin_dashboard_config.html:7-11`).
- Se várias fontes têm colunas com o mesmo nome, elas aparecem duplicadas/triplicadas porque o HTML bruto lista uma opção por fonte. A deduplicação de `dashboardFieldOptions()` não roda; mesmo quando roda, ela deduplica apenas por `sourceId` (`templates/admin_dashboard_config.html:885-889`).

### 2e. Fluxo ao salvar novo widget

Fluxo esperado:

1. Submit do form `data-new-widget-form` é interceptado (`templates/admin_dashboard_config.html:827-831`).
2. `fetch` envia `POST /admin/dashboard-config/widgets` com headers CSRF/AJAX e `body: new FormData(form)` (`templates/admin_dashboard_config.html:832-839`).
3. Frontend faz `response.json()` (`templates/admin_dashboard_config.html:840`).
4. Se `data.success` falso, exibe `alert` (`templates/admin_dashboard_config.html:841-844`).
5. Se sucesso, cria card simplificado "Recarregue para editar", reseta form, atualiza contador/bulk/aba/toast (`templates/admin_dashboard_config.html:846-873`).

O que acontece de fato:

- O listener de submit novo widget não é registrado porque fica depois da quebra em `templates/admin_dashboard_config.html:709`; portanto o form pode seguir submit normal ou não ter comportamento AJAX, dependendo do navegador/estado.
- Mesmo se o listener rodasse, há risco de backend não parsear `FormData` multipart porque `form_data()` usa `parse_qs` de corpo URL-encoded (`app/routes/common.py:12-15`).
- Em caso de erro backend, `dashboard_widget_context()` retorna HTML (`app/routes/dashboard.py:1636-1683`), mas o frontend tenta JSON sem `catch` (`templates/admin_dashboard_config.html:840-844`), gerando erro silencioso no console.

### 2f. Fluxo ao editar widget existente

Fluxo esperado:

1. Botão Editar chama `openWidgetEditor(widgetId)` (`templates/admin_dashboard_config.html:700-702`).
2. `openWidgetEditor()` chama `initDashboardWidgetForms(card)`, exibe `.widget-edit-body` e move o card para o topo (`templates/admin_dashboard_config.html:687-698`).
3. O form inline já vem com valores atuais: Fonte A selecionada (`templates/admin_dashboard_config.html:457-463`), Fonte B selecionada se `widget.source_id_b` existir (`templates/admin_dashboard_config.html:465-471`), campos label/value/series/time selecionados pela macro (`templates/admin_dashboard_config.html:489-493`), `meta_gauge` e demais campos (`templates/admin_dashboard_config.html:524-545`).
4. `syncDashboardTypeFields()` deveria mostrar Fonte B quando o tipo salvo é `gauge`/`comparativo` (`templates/admin_dashboard_config.html:917-935`).
5. Submit inline deveria ser AJAX no listener registrado por `initDashboardWidgetForms()` (`templates/admin_dashboard_config.html:947-976`).

O que acontece de fato:

- Como o script quebra em `templates/admin_dashboard_config.html:709`, `openWidgetEditor` foi definido e os listeners de Editar/Cancelar foram registrados antes da quebra (`templates/admin_dashboard_config.html:687-706`), mas `initDashboardWidgetForms` ainda não foi definido (`templates/admin_dashboard_config.html:937`). Ao clicar Editar, `openWidgetEditor()` tenta chamar uma função inexistente (`templates/admin_dashboard_config.html:694`), causando erro JS.
- A Fonte B "aparece após salvar mas não antes" é compatível com o backend persistindo `source_id_b` (`app/routes/dashboard.py:1726`) e o HTML vindo selecionado na próxima renderização (`templates/admin_dashboard_config.html:470`), mas o campo continua escondido até `syncDashboardTypeFields()` rodar, o que não acontece por causa da quebra em `templates/admin_dashboard_config.html:709`.

### 2g. Salvar gauge/comparativo quando já existe outro widget

- O frontend envia os mesmos campos do form, incluindo `widget_type=gauge|comparativo`, `source_id`, `source_id_b`, `meta_gauge`, `cor_secundaria` (`templates/admin_dashboard_config.html:313-416`, `templates/admin_dashboard_config.html:436-552`).
- Não há regra no backend que bloqueie duplicidade por dashboard/tipo/fonte (`app/routes/dashboard.py:1701-1749`, `app/routes/dashboard.py:1897-1956`; modelo em `app/models.py:248-287`).
- Se o erro observado acontece nesse cenário, a causa mais provável no código é o fluxo AJAX/form, não unicidade: script quebrado em `templates/admin_dashboard_config.html:709`; `FormData` multipart incompatível com `form_data()` em `app/routes/common.py:12-15`; retorno HTML tratado como JSON sem `catch` em `templates/admin_dashboard_config.html:840-844` e `templates/admin_dashboard_config.html:960-964`.
- Não há `logger.exception` específico no create/update de widget para esse caso; erros de validação retornam contexto HTML (`app/routes/dashboard.py:1917-1918`, `app/routes/dashboard.py:1950-1951`) e erros de banco no `db.commit()` não são capturados (`app/routes/dashboard.py:1920`, `app/routes/dashboard.py:1952`).

## 3. Modelo - `app/models.py`

### 3a. `DashboardWidget`

| Campo | Tipo | Nullable | Default | Descrição |
|---|---|---:|---|---|
| `id` | `Integer` | não | PK | Identificador (`app/models.py:251`) |
| `dashboard_id` | FK `dashboards.id` | sim | null | Dashboard dono do widget (`app/models.py:252`) |
| `source_id` | FK `dashboard_sources.id` | não | sem default | Fonte A obrigatória (`app/models.py:253`) |
| `source_id_b` | FK `dashboard_sources.id` | sim | `None` | Fonte B opcional para gauge/comparativo (`app/models.py:254-258`) |
| `title` | `String(160)` | não | sem default | Título (`app/models.py:259`) |
| `widget_type` | `String(30)` | não | `"barra"` | Tipo (`app/models.py:260`) |
| `label_field` | `String(120)` | sim | null | Campo de rótulo/agrupamento (`app/models.py:261`) |
| `value_field` | `String(120)` | sim | null | Campo numérico para agregações não-count (`app/models.py:262`) |
| `series_field` | `String(120)` | sim | null | Campo de série (`app/models.py:263`) |
| `time_field` | `String(120)` | sim | null | Campo temporal específico do widget (`app/models.py:264`) |
| `aggregation` | `String(20)` | não | `"count"` | Agregação (`app/models.py:265`) |
| `top_n` | `Integer` | não | `10` | Limite de linhas/categorias (`app/models.py:266`) |
| `color` | `String(30)` | não | `"blue"` | Cor principal (`app/models.py:267`) |
| `icone` | `String(80)` | sim | null | Ícone KPI/comparativo (`app/models.py:268`) |
| `comparar_com` | `String(30)` | sim | null | Período comparativo (`app/models.py:269`) |
| `meta_gauge` | `Float` | sim | null | Meta percentual do gauge (`app/models.py:270`) |
| `cor_secundaria` | `String(30)` | sim | null | Cor secundária para Fonte B/comparativo (`app/models.py:271`) |
| `sort_order` | `Integer` | não | `100` | Ordenação (`app/models.py:272`) |
| `size` | `String(20)` | não | `"medio"` | Tamanho visual (`app/models.py:273`) |
| `created_at` | `DateTime` | não | `datetime.utcnow` | Criação (`app/models.py:274`) |
| `updated_at` | `DateTime` | não | `datetime.utcnow`, `onupdate` | Atualização (`app/models.py:275-277`) |

### 3b. `DashboardSource`

| Campo | Tipo | Nullable | Default |
|---|---|---:|---|
| `id` | `Integer` | não | PK (`app/models.py:229`) |
| `name` | `String(160)` | não | sem default (`app/models.py:230`) |
| `source_table` / coluna `tabela_origem` | `String(120)` | não | unique/index, sem default (`app/models.py:231`) |
| `description` | `Text` | sim | null (`app/models.py:232`) |
| `is_active` | `Boolean` | não | `False` (`app/models.py:233`) |
| `default_time_field` | `String(120)` | sim | `"created_at"` (`app/models.py:234`) |
| `category` | `String(120)` | sim | null (`app/models.py:235`) |
| `note` | `Text` | sim | null (`app/models.py:236`) |
| `created_at` | `DateTime` | não | `datetime.utcnow` (`app/models.py:237`) |
| `updated_at` | `DateTime` | não | `datetime.utcnow`, `onupdate` (`app/models.py:238-240`) |

### 3c. Relação Widget/Source

- `DashboardSource.widgets` usa `DashboardWidget.source_id` como FK principal (`app/models.py:242-245`).
- `DashboardWidget.source` é relação obrigatória pela FK `source_id` (`app/models.py:279-282`).
- `DashboardWidget.source_b` é relação opcional pela FK `source_id_b` (`app/models.py:283-286`).
- `DashboardWidget.dashboard` relaciona com `Dashboard.widgets`, que tem cascade `all, delete-orphan` (`app/models.py:127-130`, `app/models.py:287`).

## 4. Regras de negócio

### 4a. Tipos e campos obrigatórios

Tipos aceitos: `barra`, `barra_horizontal`, `linha`, `pizza`, `area`, `kpi`, `tabela`, `gauge`, `comparativo` (`app/routes/dashboard.py:79`).

Regras implementadas:

- Todos precisam de `source_id` válido e ativo (`app/routes/dashboard.py:1907-1909`, `app/routes/dashboard.py:1940-1942`).
- `source_id_b` é opcional; se enviado, precisa ser válido e ativo (`app/routes/dashboard.py:1910-1913`, `app/routes/dashboard.py:1943-1946`).
- `gauge`: backend força `aggregation = "count"` (`app/routes/dashboard.py:1719`); não exige `source_id_b`; pode usar `meta_gauge` (`app/routes/dashboard.py:1741`).
- `comparativo`: pode usar `source_id_b`; se não houver, compara por período via `comparar_com` (`app/routes/dashboard.py:401-447`).
- `kpi`: usa `apply_widget_comparison` como comparativo por período (`app/routes/dashboard.py:526-531`).
- Gráficos/tabela (`barra`, `barra_horizontal`, `linha`, `pizza`, `area`, `tabela`): usam `label_field`/`series_field` se presentes; sem eles, a query agrega total sem `GROUP BY` (`app/routes/dashboard.py:536-559`).
- `value_field` só é obrigatório quando `aggregation != "count"` (`app/routes/dashboard.py:1722-1723`).

### 4b. Campo temporal

Precedência implementada:

1. `widget.time_field` (`app/routes/dashboard.py:318-320`).
2. `widget.source.default_time_field` (`app/routes/dashboard.py:318-324`).
3. Colunas de controle: `created_at`, `solved_at`, `reference_date` (`app/routes/dashboard.py:327-329`).
4. Aliases de `CONTROL_COLUMN_ALIASES` (`app/routes/dashboard.py:330-332`).
5. Aliases explícitos `CreatedDate`, `CompletedDate` (`app/routes/dashboard.py:333-335`).
6. Primeira coluna `datetime/date/timestamp` encontrada (`app/routes/dashboard.py:204-208`, `app/routes/dashboard.py:336`).

### 4c. Cores

- Valores aceitos: `blue`, `green`, `amber`, `red`, `violet`, `slate` (`app/routes/dashboard.py:81`).
- `color` é cor principal; inválida vira `"blue"` (`app/routes/dashboard.py:1735-1736`).
- `cor_secundaria` é usada apenas para `gauge` e `comparativo`; inválida/ausente vira `"red"` (`app/routes/dashboard.py:1742-1746`).
- No dashboard visual, `color` vira `cor_widget` (`templates/dashboard.html:40`) e `cor_secundaria` vira `cor_secundaria` (`templates/dashboard.html:41`). Gauge com Fonte B usa cor secundária no arco preenchido (`templates/dashboard.html:86-91`); comparativo usa cor secundária no valor comparativo (`templates/dashboard.html:137-146`).

### 4d. `meta_gauge`

- Representa meta percentual do gauge (`app/models.py:270`; input em `templates/admin_dashboard_config.html:398-400`, `templates/admin_dashboard_config.html:532-534`).
- É normalizada por `dashboard_float(..., 0, 100)` (`app/routes/dashboard.py:110-118`, `app/routes/dashboard.py:1741`).
- Com Fonte B: percentual = `total_b / total_a * 100` (`app/routes/dashboard.py:449-453`).
- Sem Fonte B: percentual = `filtered / total * 100`; `filtered` aplica período se houver `time_field` (`app/routes/dashboard.py:471-473`).
- Status: `ok` se percentual >= meta; `atencao` se percentual >= 85% da meta; `critico` abaixo disso (`app/routes/dashboard.py:455-461`, `app/routes/dashboard.py:475-481`).
- Texto visual: `Dentro da meta`, `Atenção - próximo do limite`, `Abaixo da meta` (`templates/dashboard.html:110-113`).

## 5. Dependências e relacionamentos

### 5a. Widget depende de fontes e dashboard

- `source_id` é obrigatório no modelo (`app/models.py:253`).
- `source_id_b` é opcional (`app/models.py:254-258`).
- `dashboard_id` é nullable no modelo (`app/models.py:252`), mas o create exige dashboard válido (`app/routes/dashboard.py:1903-1906`). Existe rotina para migrar órfãos para "Dashboard principal" (`app/main.py:215-226`).
- Se uma `DashboardSource` é excluída via rota própria, as dependências incluem widgets onde ela é Fonte A ou B (`app/deletion_dependencies.py:20-37`) e a exclusão exige cascata se houver widgets (`app/routes/dashboard.py:1606-1613`); com cascata, `delete_widgets` remove os widgets (`app/routes/dashboard.py:1620-1621`, `app/deletion_dependencies.py:134-143`).

### 5b. DashboardSource depende de tabela local

- `DashboardSource.source_table` mapeia a coluna `tabela_origem`, unique/index (`app/models.py:231`).
- `sync_dashboard_sources` cria fontes para tabelas existentes, mas não remove fontes antigas quando a tabela desaparece (`app/routes/dashboard.py:223-238`). Há teste cobrindo isso (`tests/test_dashboard_sources.py:5-17`).
- Se a tabela for excluída, a fonte permanece cadastrada, mas `dashboard_source_table_exists` passa a retornar falso (`app/routes/dashboard.py:156-173`), e payload mostra `table_exists: false` / `status_badge: "Tabela não encontrada"` (`app/routes/dashboard.py:248-257`).

### 5c. Detecção de fonte com tabela inexistente

- `dashboard_source_table_exists(table_name)` consulta `information_schema.TABLES` (`app/routes/dashboard.py:156-169`).
- `dashboard_source_payload()` usa essa função para `table_exists`, `row_count` e `status_badge` (`app/routes/dashboard.py:248-257`).
- O Config Dashboard só permite fontes ativas no seletor (`app/routes/dashboard.py:1637-1641`), mas create/update ainda validam existência da tabela (`app/routes/dashboard.py:1907-1909`, `app/routes/dashboard.py:1940-1942`, e Fonte B em `app/routes/dashboard.py:1910-1913`, `app/routes/dashboard.py:1943-1946`).
- `load_widget_data()` também bloqueia renderização se Fonte A/B estiver indisponível (`app/routes/dashboard.py:502-511`).

## 6. Problemas identificados

### 6a. Fonte B não aparece ao selecionar `gauge` no formulário de novo widget

Linha exata principal: `templates/admin_dashboard_config.html:709`.

Motivo: o script inline usa `new bootstrap.Modal(deleteModalEl)` antes de Bootstrap estar carregado. O Bootstrap bundle só entra em `templates/base.html:108`, depois do bloco `content` onde está o script (`templates/base.html:105-108`). A execução para na linha 709, então `syncDashboardTypeFields()` (`templates/admin_dashboard_config.html:917-935`) e o listener de `change` (`templates/admin_dashboard_config.html:981-989`) nunca são registrados. O campo Fonte B permanece escondido por `style="display:none"` (`templates/admin_dashboard_config.html:330`).

### 6b. Campos label/valor/série/temporal com colunas duplicadas/triplicadas de outras fontes

Linhas exatas:

- Macro renderiza todas as colunas de todas as fontes dentro de cada select (`templates/admin_dashboard_config.html:7-11`).
- A filtragem que deveria limitar por fonte fica em `syncDashboardSourceFields()` (`templates/admin_dashboard_config.html:902-915`).
- Essa filtragem não roda porque o script quebra em `templates/admin_dashboard_config.html:709`.

Resultado: o HTML bruto lista opções de todas as fontes. Se fontes diferentes têm colunas com o mesmo nome, elas aparecem repetidas. A deduplicação prevista só ocorreria no cache por fonte (`templates/admin_dashboard_config.html:885-889`) e não é executada.

### 6c. Ao salvar/excluir widget a aba volta para a primeira

Linhas relevantes:

- Estado da aba é salvo em `sessionStorage` (`templates/admin_dashboard_config.html:593-613`).
- Após AJAX, o código tenta restaurar via `reactivateDashboardTab(activeDashboardId)` (`templates/admin_dashboard_config.html:599-605`, `templates/admin_dashboard_config.html:871-872`, `templates/admin_dashboard_config.html:820-821`).
- Porém o script quebra em `templates/admin_dashboard_config.html:709`; os handlers AJAX de salvar/excluir widget não são registrados (`templates/admin_dashboard_config.html:756-875`).
- Sem AJAX/estado preservado, o backend redireciona usando `dashboard_config_path(dashboard_id)` quando consegue (`app/routes/dashboard.py:1924`, `app/routes/dashboard.py:1956`, `app/routes/dashboard.py:1974`). Se o erro retorna `dashboard_widget_context` sem o `user` no create/update, a lista de dashboards pode cair para `db.query(Dashboard)` e escolher o primeiro quando `dashboard_id` não é preservado corretamente (`app/routes/dashboard.py:1636-1656`, `app/routes/dashboard.py:1906-1918`, `app/routes/dashboard.py:1942-1951`).

### 6d. Ações em massa não funcionam

Linha exata principal: `templates/admin_dashboard_config.html:709`.

O bloco que define e inicializa bulk actions fica depois da quebra:

- Variáveis e funções de bulk: `templates/admin_dashboard_config.html:1007-1076`.
- Inicialização em `DOMContentLoaded` ou imediata: `templates/admin_dashboard_config.html:1078-1084`.
- Botão Aplicar depende do listener em `templates/admin_dashboard_config.html:1057-1073`.

Como nada disso roda, `Selecionar todos`, contador e botão Aplicar ficam sem comportamento.

Há outro ponto de permissão: o endpoint AJAX `/dashboard/widgets/bulk-delete` exige `require_admin` (`app/routes/dashboard.py:1977-1981`), enquanto a tela Config Dashboard exige `require_dashboard` (`app/routes/dashboard.py:1753-1756`). Um usuário com permissão de dashboard, mas sem `is_admin`, recebe 403 no bulk delete AJAX.

### 6e. Não é possível salvar gauge/comparativo quando já existe widget cadastrado

Conclusão pelo código:

- Não é validação de unicidade no backend: não há constraint no modelo `DashboardWidget` (`app/models.py:248-287`) nem checagem nas rotas (`app/routes/dashboard.py:1897-1956`) ou em `apply_dashboard_widget()` (`app/routes/dashboard.py:1701-1749`).
- Não é regra específica de `gauge`/`comparativo`: `source_id_b` é opcional e só é associado quando tipo é `gauge`/`comparativo` (`app/routes/dashboard.py:1726`); `cor_secundaria` tem fallback (`app/routes/dashboard.py:1742-1746`).
- O erro mais provável é JS/serialização:
  - Script quebra em `templates/admin_dashboard_config.html:709`, impedindo listener de submit (`templates/admin_dashboard_config.html:827-875`, `templates/admin_dashboard_config.html:947-976`).
  - Quando AJAX roda, envia `new FormData(form)` (`templates/admin_dashboard_config.html:832-839`, `templates/admin_dashboard_config.html:952-959`), mas o backend parseia como query string com `parse_qs` (`app/routes/common.py:12-15`).
  - Se o backend retorna erro de validação, retorna HTML via `dashboard_widget_context()` (`app/routes/dashboard.py:1906-1918`, `app/routes/dashboard.py:1942-1951`), e o frontend chama `response.json()` sem tratamento de exceção (`templates/admin_dashboard_config.html:840-844`, `templates/admin_dashboard_config.html:960-964`).
- O erro não é exibido ao usuário quando `response.json()` falha; não há `.catch`.
- Não há log explícito no servidor para erro de validação de widget; apenas consultas de tabela/coluna usam `logger.exception` (`app/routes/dashboard.py:151-197`). Erros de `db.commit()` de create/update não são capturados (`app/routes/dashboard.py:1920`, `app/routes/dashboard.py:1952`).

## Resumo das causas raiz

1. `templates/admin_dashboard_config.html:709` executa Bootstrap antes de `templates/base.html:108`, quebrando todo o JS posterior da tela.
2. `templates/admin_dashboard_config.html:7-11` renderiza todas as colunas de todas as fontes; depende do JS posterior para filtrar, mas o JS não roda.
3. `templates/admin_dashboard_config.html:832-839` e `templates/admin_dashboard_config.html:952-959` enviam `FormData` multipart, enquanto `app/routes/common.py:12-15` espera corpo URL-encoded.
4. `app/routes/dashboard.py:1906-1918` e `app/routes/dashboard.py:1942-1951` retornam HTML em erros de widget, enquanto o frontend espera JSON (`templates/admin_dashboard_config.html:840-844`, `templates/admin_dashboard_config.html:960-964`).
5. `/dashboard/widgets/bulk-delete` exige admin (`app/routes/dashboard.py:1977-1981`), diferente da permissão da página (`app/routes/dashboard.py:1753-1756`).
