from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.database import Base


user_portal_groups = Table(
    "user_portal_groups",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("group_id", ForeignKey("portal_groups.id"), primary_key=True),
)


user_report_categories = Table(
    "user_report_categories",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("category_id", ForeignKey("report_categories.id"), primary_key=True),
)


portal_group_report_categories = Table(
    "portal_group_report_categories",
    Base.metadata,
    Column("group_id", ForeignKey("portal_groups.id"), primary_key=True),
    Column("category_id", ForeignKey("report_categories.id"), primary_key=True),
)


dashboard_categories = Table(
    "dashboard_categories",
    Base.metadata,
    Column("dashboard_id", Integer, ForeignKey("dashboards.id"), primary_key=True),
    Column("category_id", Integer, ForeignKey("report_categories.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    auth_source: Mapped[str] = mapped_column(String(20), default="local", nullable=False)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_relatorios: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_dashboard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_importacao: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_usuarios: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    executions: Mapped[list["ReportExecution"]] = relationship(back_populates="user")
    portal_groups: Mapped[list["PortalGroup"]] = relationship(
        secondary=user_portal_groups, back_populates="users"
    )
    report_categories: Mapped[list["ReportCategory"]] = relationship(
        secondary=user_report_categories, back_populates="users"
    )

    @property
    def portal_admin(self) -> bool:
        return bool(self.is_admin)

    @property
    def portal_view(self) -> bool:
        return any(group.name == "portal_view" for group in self.portal_groups)


class PortalGroup(Base):
    __tablename__ = "portal_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    users: Mapped[list[User]] = relationship(secondary=user_portal_groups, back_populates="portal_groups")
    report_categories: Mapped[list["ReportCategory"]] = relationship(
        secondary=portal_group_report_categories, back_populates="portal_groups"
    )


class ReportCategory(Base):
    __tablename__ = "report_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    users: Mapped[list[User]] = relationship(secondary=user_report_categories, back_populates="report_categories")
    portal_groups: Mapped[list[PortalGroup]] = relationship(
        secondary=portal_group_report_categories, back_populates="report_categories"
    )
    dashboards: Mapped[list["Dashboard"]] = relationship(
        secondary=dashboard_categories, back_populates="categories"
    )


class Dashboard(Base):
    __tablename__ = "dashboards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    categories: Mapped[list[ReportCategory]] = relationship(
        "ReportCategory",
        secondary=dashboard_categories,
        back_populates="dashboards",
    )
    widgets: Mapped[list["DashboardWidget"]] = relationship(
        back_populates="dashboard",
        cascade="all, delete-orphan",
        primaryjoin=lambda: Dashboard.id == foreign(DashboardWidget.dashboard_id),
    )


class LdapConfig(Base):
    __tablename__ = "ldap_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    server: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    port: Mapped[str] = mapped_column(String(10), default="389", nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_dn: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    bind_dn: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    bind_password: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    user_filter: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    last_connection_status: Mapped[Optional[str]] = mapped_column(String(40))
    last_connection_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class AuthLog(Base):
    __tablename__ = "auth_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(LONGTEXT)
    sql_query: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    destination_table: Mapped[Optional[str]] = mapped_column(String(120))
    modo_salvamento: Mapped[str] = mapped_column(String(20), default="substituir", nullable=False)
    campo_sql_periodo: Mapped[Optional[str]] = mapped_column(String(255))
    modo_filtro_periodo: Mapped[str] = mapped_column(String(30), default="filtro_externo", nullable=False)
    clausula_periodo_original: Mapped[Optional[str]] = mapped_column(Text)
    query_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    filter_timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=30)
    report_type: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    builder_config: Mapped[Optional[str]] = mapped_column(LONGTEXT)
    schedule_auto: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_horarios: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    is_primary = Column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dashboard_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(120))
    card_color: Mapped[Optional[str]] = mapped_column(String(30))
    icon: Mapped[Optional[str]] = mapped_column(String(80))
    executive_highlight: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    executions: Mapped[list["ReportExecution"]] = relationship(back_populates="report")


class ReportExecution(Base):
    __tablename__ = "report_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reports.id"), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    sql_query: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    destination_table: Mapped[Optional[str]] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(LONGTEXT)
    result_json: Mapped[Optional[str]] = mapped_column(LONGTEXT)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    report: Mapped[Optional[Report]] = relationship(back_populates="executions")
    user: Mapped[Optional[User]] = relationship(back_populates="executions")


class TempReportResult(Base):
    __tablename__ = "temp_report_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    temp_table: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_column: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")


class DashboardSource(Base):
    __tablename__ = "dashboard_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_table: Mapped[str] = mapped_column("tabela_origem", String(120), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_time_field: Mapped[Optional[str]] = mapped_column(String(120), default="created_at")
    category: Mapped[Optional[str]] = mapped_column(String(120))
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    widgets: Mapped[list["DashboardWidget"]] = relationship(
        back_populates="source",
        foreign_keys="DashboardWidget.source_id",
    )


class DashboardWidget(Base):
    __tablename__ = "dashboard_widgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    dashboard_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("dashboard_sources.id"), nullable=False, index=True)
    source_id_b: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        default=None,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    widget_type: Mapped[str] = mapped_column(String(30), default="barra", nullable=False)
    label_field: Mapped[Optional[str]] = mapped_column(String(120))
    value_field: Mapped[Optional[str]] = mapped_column(String(120))
    series_field: Mapped[Optional[str]] = mapped_column(String(120))
    time_field: Mapped[Optional[str]] = mapped_column(String(120))
    aggregation: Mapped[str] = mapped_column(String(20), default="count", nullable=False)
    top_n: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    color: Mapped[str] = mapped_column(String(30), default="blue", nullable=False)
    icone: Mapped[Optional[str]] = mapped_column(String(80))
    comparar_com: Mapped[Optional[str]] = mapped_column(String(30))
    meta_gauge: Mapped[Optional[float]] = mapped_column(Float)
    cor_secundaria: Mapped[Optional[str]] = mapped_column(String(30))
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    size: Mapped[str] = mapped_column(String(20), default="medio", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    source: Mapped[DashboardSource] = relationship(
        back_populates="widgets",
        foreign_keys=[source_id],
    )
    source_b: Mapped[Optional[DashboardSource]] = relationship(
        "DashboardSource",
        foreign_keys=[source_id_b],
        primaryjoin=lambda: DashboardSource.id == foreign(DashboardWidget.source_id_b),
    )
    dashboard: Mapped[Optional[Dashboard]] = relationship(
        back_populates="widgets",
        primaryjoin=lambda: Dashboard.id == foreign(DashboardWidget.dashboard_id),
    )


class ConnectorConfig(Base):
    __tablename__ = "connector_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="glpi")
    db_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default="mysql")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[str] = mapped_column(String(10), default="3306", nullable=False)
    database_name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_database: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, default="glpi_local")
    table_prefix: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, default="glpi_")
    schema_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    initial_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    suggested_frequency: Mapped[str] = mapped_column(String(80), default="08:00,20:00", nullable=False)
    full_schedule_horarios: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    full_schedule_dias: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    import_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="automatic")
    table_whitelist: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def whitelist_tables(self) -> list[str]:
        """Retorna lista de tabelas da whitelist ou lista vazia se automatico."""
        if self.import_mode != "custom":
            return []
        if not self.table_whitelist:
            return []
        try:
            import json

            result = json.loads(self.table_whitelist)
            return result if isinstance(result, list) else []
        except Exception:
            return []


class ConnectorSyncTable(Base):
    __tablename__ = "connector_sync_tables"
    __table_args__ = (
        UniqueConstraint("table_name", "connector_type", name="uq_connector_table"),
        Index("idx_table_name", "table_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="glpi")
    table_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    load_type: Mapped[str] = mapped_column(String(20), default="incremental", nullable=False)
    incremental_column: Mapped[Optional[str]] = mapped_column(String(120))
    configured_load_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    effective_load_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    effective_incremental_column: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    runs: Mapped[list["ConnectorRun"]] = relationship(back_populates="sync_table")


class ConnectorRun(Base):
    __tablename__ = "connector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="glpi")
    sync_table_id: Mapped[Optional[int]] = mapped_column(ForeignKey("connector_sync_tables.id"), nullable=True)
    table_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    sync_table: Mapped[Optional[ConnectorSyncTable]] = relationship(back_populates="runs")


class GlpiImportLog(Base):
    __tablename__ = "glpi_import_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("connector_runs.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DbUser(Base):
    __tablename__ = "db_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    host_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    target_database: Mapped[str] = mapped_column(String(120), nullable=False)
    permission: Mapped[str] = mapped_column(String(20), nullable=False, default="SELECT")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    databases: Mapped[list["DbUserDatabase"]] = relationship(
        back_populates="db_user",
        cascade="all, delete-orphan",
        order_by="DbUserDatabase.target_database",
    )


class DbUserDatabase(Base):
    __tablename__ = "db_user_databases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    db_user_id: Mapped[int] = mapped_column(ForeignKey("db_users.id"), nullable=False)
    target_database: Mapped[str] = mapped_column(String(120), nullable=False)

    db_user: Mapped[DbUser] = relationship(back_populates="databases")

    __table_args__ = (
        UniqueConstraint("db_user_id", "target_database", name="uq_db_user_database"),
    )


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    report_name: Mapped[Optional[str]] = mapped_column(String(160))
    table_name: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
