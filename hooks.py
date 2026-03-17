# -*- coding: utf-8 -*-
"""Añade columnas de ghr_compliance a res_partner si no existen (install/upgrade)."""


def _add_res_partner_columns(cr):
    """Idempotent: añade columnas de cumplimiento a res_partner."""
    columns_sql = [
        ("occupation", "VARCHAR"),
        ("origin_funds", "TEXT"),
        ("is_pep", "BOOLEAN DEFAULT FALSE"),
        ("nationality_country_id", "INTEGER REFERENCES res_country(id) ON DELETE SET NULL"),
        ("monthly_salary", "DOUBLE PRECISION"),
        ("other_income", "DOUBLE PRECISION"),
        ("compliance_notes", "TEXT"),
        ("last_compliance_assessment_id", "INTEGER REFERENCES compliance_assessment(id) ON DELETE SET NULL"),
    ]
    # Timeout corto para no bloquear el upgrade; si hay lock, falla rápido
    try:
        cr.execute("SET lock_timeout = '8s'")
    except Exception:
        pass
    try:
        for col, col_type in columns_sql:
            cr.execute(
                'ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS "%s" %s' % (col, col_type)
            )
    finally:
        try:
            cr.execute("RESET lock_timeout")
        except Exception:
            pass


def post_init_hook(cr, registry):
    """Se ejecuta tras instalar el módulo."""
    _add_res_partner_columns(cr)
