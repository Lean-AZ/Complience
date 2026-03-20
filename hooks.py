# -*- coding: utf-8 -*-
"""Añade columnas de ghr_compliance a res_partner si no existen (install/upgrade)."""

from odoo import api, SUPERUSER_ID


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


def _configure_pep_trigger_questions(cr):
    """Configura preguntas condicionales PEP en la encuesta KYC PF.

    - Si responde "Sí" a "¿Es o ha sido PEP o figura pública?", se muestran:
      Cargo/Fecha/Institución/País PEP.
    - Si responde "Sí" a "¿Tiene vínculo con PEP o figura pública?", se muestran:
      Nombre/Tipo/Cargo/Fecha/Institución/País de la PEP vinculada.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Question = env["survey.question"].sudo()
    Answer = env["survey.question.answer"].sudo()

    survey = env["survey.survey"].sudo().search(
        [("title", "=", "KYC Persona Física - Debida Diligencia")], limit=1
    )
    if not survey:
        return

    def _question_by_title(title):
        return Question.search(
            [
                ("survey_id", "=", survey.id),
                ("is_page", "=", False),
                ("title", "=", title),
            ],
            limit=1,
        )

    def _yes_answer_for_question(question):
        if not question:
            return Answer.browse()
        return Answer.search(
            [
                ("question_id", "=", question.id),
                ("value", "in", ["Sí", "Si", "sí", "si", "Yes", "YES", "yes"]),
            ],
            limit=1,
        )

    pep_main_q = _question_by_title("¿Es o ha sido PEP o figura pública?")
    pep_link_q = _question_by_title("¿Tiene vínculo con PEP o figura pública?")
    pep_main_yes = _yes_answer_for_question(pep_main_q)
    pep_link_yes = _yes_answer_for_question(pep_link_q)

    if not pep_main_yes or not pep_link_yes:
        return

    pep_dependents = [
        "Cargo, Rango o Posición PEP",
        "Fecha desde que ocupa el cargo PEP",
        "Institución PEP",
        "País PEP",
    ]
    pep_link_dependents = [
        "Nombre de la PEP vinculada",
        "Tipo de Vinculación con PEP",
        "Cargo de la PEP vinculada",
        "Fecha desde que ocupa cargo (PEP vinculada)",
        "Institución de la PEP vinculada",
        "País de la PEP vinculada",
    ]

    # Odoo 17+ suele usar triggering_answer_ids (sin is_conditional).
    # En algunas variantes el campo puede llamarse suggested_answer_ids.
    trigger_field = None
    if "triggering_answer_ids" in Question._fields:
        trigger_field = "triggering_answer_ids"
    elif "suggested_answer_ids" in Question._fields:
        trigger_field = "suggested_answer_ids"
    else:
        return

    for title in pep_dependents:
        q = _question_by_title(title)
        if q:
            q.write({trigger_field: [(6, 0, [pep_main_yes.id])]})

    for title in pep_link_dependents:
        q = _question_by_title(title)
        if q:
            q.write({trigger_field: [(6, 0, [pep_link_yes.id])]})


def post_init_hook(cr, registry):
    """Se ejecuta tras instalar el módulo."""
    _add_res_partner_columns(cr)

    # Limpia mapeos obsoletos automáticamente:
    # si eliminamos preguntas del template (survey), los mapeos anteriores también deben desaparecer
    # para que el operador no los vea marcados/rojos en la interfaz.
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env["compliance.question.mapping"].sudo().action_clean_obsolete_mappings()
    except Exception:
        # No bloquear upgrade si la limpieza falla; el botón existe igualmente en UI.
        pass

    # Configura condicionales de preguntas PEP en encuesta KYC.
    try:
        _configure_pep_trigger_questions(cr)
    except Exception:
        # No bloquear upgrade si la configuración condicional falla.
        pass
