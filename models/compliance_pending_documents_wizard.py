# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class CompliancePendingDocumentsWizard(models.TransientModel):
    _name = "compliance.pending.documents.wizard"
    _description = "Subir documentos pendientes KYC (R-19)"

    assessment_id = fields.Many2one(
        "compliance.assessment",
        string="Evaluación",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    line_ids = fields.One2many(
        "compliance.pending.documents.wizard.line",
        "wizard_id",
        string="Documentos pendientes",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        assessment_id = self.env.context.get("default_assessment_id") or self.env.context.get("active_id")
        if not assessment_id:
            return res

        assessment = self.env["compliance.assessment"].browse(assessment_id)
        if not assessment.exists():
            return res

        pending_docs = self.env["compliance.kyc.pending.document"].sudo().search(
            [
                ("assessment_id", "=", assessment.id),
                ("status", "=", "pending"),
            ],
            order="deadline asc, id asc",
        )

        res["assessment_id"] = assessment.id
        res["line_ids"] = [(0, 0, {"pending_document_id": doc.id}) for doc in pending_docs]
        return res

    def action_confirm(self):
        """Sube adjuntos y marca los pending como recibidos."""
        self.ensure_one()
        assessment = self.assessment_id
        PendingDocLine = self.line_ids
        Attachment = self.env["ir.attachment"].sudo()

        received_count = 0
        for line in PendingDocLine:
            doc = line.pending_document_id
            if not doc or doc.status != "pending":
                continue
            if not line.file_data:
                continue

            filename = (line.file_name or doc.title or _("Documento")).strip()
            mimetype = line.mimetype or "application/octet-stream"

            existing = Attachment.search(
                [
                    ("res_model", "=", "compliance.assessment"),
                    ("res_id", "=", assessment.id),
                    ("name", "=", filename),
                ],
                limit=1,
            )
            vals_attach = {
                "name": filename,
                "type": "binary",
                "datas": line.file_data,
                "mimetype": mimetype,
                "res_model": "compliance.assessment",
                "res_id": assessment.id,
            }
            if existing:
                existing.write(vals_attach)
            else:
                Attachment.create(vals_attach)

            doc.action_mark_received()
            received_count += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Documentos actualizados"),
                "message": _("%s documento(s) marcado(s) como recibido(s).") % received_count,
                "type": "success",
                "sticky": False,
            },
            "next": {"type": "ir.actions.act_window_close"},
        }


class CompliancePendingDocumentsWizardLine(models.TransientModel):
    _name = "compliance.pending.documents.wizard.line"
    _description = "Línea de documentos pendientes (R-19)"

    wizard_id = fields.Many2one(
        "compliance.pending.documents.wizard",
        string="Wizard",
        required=True,
        ondelete="cascade",
    )
    pending_document_id = fields.Many2one(
        "compliance.kyc.pending.document",
        string="Documento pendiente",
        required=True,
        ondelete="cascade",
        index=True,
    )

    title = fields.Char(related="pending_document_id.title", string="Título", readonly=True, store=False)
    required = fields.Boolean(related="pending_document_id.required", string="Requerido", readonly=True)
    deadline = fields.Datetime(related="pending_document_id.deadline", string="Caducidad", readonly=True)

    file_data = fields.Binary(string="Archivo")
    file_name = fields.Char(string="Nombre de archivo")
    mimetype = fields.Char(string="MIME type", default="application/octet-stream")

