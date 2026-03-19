# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _


class ComplianceKycPendingDocument(models.Model):
    _name = "compliance.kyc.pending.document"
    _description = "Documentos KYC pendientes"
    _order = "deadline asc, id asc"

    assessment_id = fields.Many2one(
        "compliance.assessment",
        string="Evaluación",
        required=True,
        ondelete="cascade",
        index=True,
    )
    survey_question_id = fields.Many2one(
        "survey.question",
        string="Documento (pregunta upload_file)",
        required=True,
        index=True,
    )
    title = fields.Char(string="Título", related="survey_question_id.title", store=True, readonly=True)

    required = fields.Boolean(string="Requerido (por criterio del módulo)")

    status = fields.Selection(
        [
            ("pending", "Pendiente (\"Adjuntaré más tarde\")"),
            ("missing", "Faltante (sin adjuntar y sin marcar más tarde)"),
            ("received", "Recibido"),
            ("expired", "Vencido"),
        ],
        string="Estado",
        required=True,
        default="pending",
        index=True,
    )

    deadline = fields.Datetime(string="Vence (caducidad)", index=True)
    received_at = fields.Datetime(string="Fecha de recepción")

    _sql_constraints = [
        (
            "uniq_assessment_question",
            "unique(assessment_id, survey_question_id)",
            "Ya existe un registro para este documento en esta evaluación.",
        )
    ]

    def action_mark_received(self):
        """Marca el/los documentos como recibidos (R-19)."""
        self.write(
            {
                "status": "received",
                "received_at": fields.Datetime.now(),
            }
        )

