from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Dashboard, DashboardSource, DashboardWidget, Report


def widget_item(widget: DashboardWidget) -> dict:
    return {
        "id": widget.id,
        "title": widget.title,
        "dashboard": widget.dashboard.name if widget.dashboard else "-",
        "type": widget.widget_type,
        "source": widget.source.name if widget.source else "-",
        "order": widget.sort_order,
    }


def source_dependencies(db: Session, source_id: int) -> dict:
    source = db.get(DashboardSource, source_id)
    widgets = (
        db.query(DashboardWidget)
        .filter(or_(DashboardWidget.source_id == source_id, DashboardWidget.source_id_b == source_id))
        .order_by(DashboardWidget.title.asc())
        .all()
    )
    return {
        "source": source,
        "widgets": widgets,
        "payload": {
            "source": {"id": source.id, "name": source.name, "table": source.source_table} if source else None,
            "widget_count": len(widgets),
            "widgets": [widget_item(widget) for widget in widgets],
            "has_dependencies": bool(widgets),
        },
    }


def report_dependencies(db: Session, report: Report) -> dict:
    table_name = (report.destination_table or "").strip()
    sources = []
    widgets = []
    if table_name:
        sources = (
            db.query(DashboardSource)
            .filter(DashboardSource.source_table == table_name)
            .order_by(DashboardSource.name.asc())
            .all()
        )
        source_ids = [source.id for source in sources]
        if source_ids:
            widgets = (
                db.query(DashboardWidget)
                .filter(or_(DashboardWidget.source_id.in_(source_ids), DashboardWidget.source_id_b.in_(source_ids)))
                .order_by(DashboardWidget.title.asc())
                .all()
            )
    return {
        "sources": sources,
        "widgets": widgets,
        "payload": {
            "report": {"id": report.id, "name": report.name, "table": table_name},
            "source_count": len(sources),
            "widget_count": len(widgets),
            "sources": [{"id": source.id, "name": source.name, "table": source.source_table} for source in sources],
            "widgets": [widget_item(widget) for widget in widgets],
            "has_dependencies": bool(sources or widgets),
        },
    }


def local_table_dependencies(db: Session, table_name: str) -> dict:
    reports = (
        db.query(Report)
        .filter(Report.destination_table == table_name)
        .order_by(Report.name.asc())
        .all()
    )
    sources = (
        db.query(DashboardSource)
        .filter(DashboardSource.source_table == table_name)
        .order_by(DashboardSource.name.asc())
        .all()
    )
    source_ids = [source.id for source in sources]
    widgets = []
    if source_ids:
        widgets = (
            db.query(DashboardWidget)
            .filter(or_(DashboardWidget.source_id.in_(source_ids), DashboardWidget.source_id_b.in_(source_ids)))
            .order_by(DashboardWidget.title.asc())
            .all()
        )
    return {
        "reports": reports,
        "sources": sources,
        "widgets": widgets,
        "payload": {
            "table": table_name,
            "report_count": len(reports),
            "source_count": len(sources),
            "widget_count": len(widgets),
            "reports": [{"id": report.id, "name": report.name} for report in reports],
            "sources": [{"id": source.id, "name": source.name, "table": source.source_table} for source in sources],
            "widgets": [widget_item(widget) for widget in widgets],
            "has_dependencies": bool(reports or sources or widgets),
        },
    }


def dashboard_dependencies(db: Session, dashboard_id: int) -> dict:
    dashboard = db.get(Dashboard, dashboard_id)
    widgets = []
    if dashboard:
        widgets = (
            db.query(DashboardWidget)
            .filter(DashboardWidget.dashboard_id == dashboard_id)
            .order_by(DashboardWidget.title.asc())
            .all()
        )
    return {
        "dashboard": dashboard,
        "widgets": widgets,
        "payload": {
            "dashboard": {"id": dashboard.id, "name": dashboard.name} if dashboard else None,
            "widget_count": len(widgets),
            "widgets": [widget_item(widget) for widget in widgets],
            "has_dependencies": bool(widgets),
        },
    }


def delete_widgets(db: Session, widgets: list[DashboardWidget]) -> int:
    seen = set()
    count = 0
    for widget in widgets:
        if widget.id in seen:
            continue
        seen.add(widget.id)
        db.delete(widget)
        count += 1
    return count

