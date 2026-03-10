# -*- coding: utf-8 -*-
import base64
import json
import logging
import os
import time
import urllib.request
import urllib.error

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ComplianceDocumentAnalysis(models.Model):
    _name = 'compliance.document.analysis'
    _description = 'Análisis documental (OCR) para cumplimiento'

    name = fields.Char(string="Nombre", compute="_compute_name", store=True)
    attachment_id = fields.Many2one('ir.attachment', string="Adjunto", required=True, ondelete='cascade')
    assessment_id = fields.Many2one('compliance.assessment', string="Evaluación", ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string="Contacto", ondelete='set null')
    ocr_text = fields.Text(string="Texto extraído (OCR)")

    @api.depends('attachment_id', 'attachment_id.name')
    def _compute_name(self):
        for rec in self:
            rec.name = rec.attachment_id.name if rec.attachment_id else ""

    @api.model
    def _call_gemini_ocr(self, api_key, image_b64, mime_type):
        """Envía imagen a Gemini y devuelve texto extraído."""
        # La API espera base64 como string; en Odoo att.datas puede venir como bytes
        if isinstance(image_b64, bytes):
            image_b64 = image_b64.decode("utf-8")
        model_name = "gemini-2.0-flash"
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model_name
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        prompt = "Extrae todo el texto visible de esta imagen. Devuelve solo el texto, sin comentarios."
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": image_b64}},
                ]
            }],
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.1},
        }
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _logger.warning("ghr_compliance Gemini OCR HTTP error: %s", e)
            return None, _("Error API: %s") % (e.code or e.reason)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            _logger.warning("ghr_compliance Gemini OCR error: %s", e)
            return None, str(e)
        candidates = data.get("candidates") or []
        if not candidates:
            return None, _("La API no devolvió respuesta.")
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return None, _("Respuesta vacía.")
        return (parts[0].get("text") or "").strip(), None


class ComplianceAssessment(models.Model):
    _inherit = 'compliance.assessment'

    document_analysis_ids = fields.One2many(
        'compliance.document.analysis',
        'assessment_id',
        string="Análisis documental",
        readonly=True,
    )
    critical_empty_fields = fields.Html(
        string="Campos críticos vacíos",
        compute="_compute_critical_empty_fields",
        sanitize=False,
    )

    @api.depends('partner_id', 'partner_id.occupation', 'partner_id.origin_funds', 'partner_id.nationality_country_id',
                 'transaccional_volume')
    def _compute_critical_empty_fields(self):
        # #region agent log
        try:
            log_path = os.path.join(os.path.dirname(__file__), "..", "debug-076c93.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "076c93", "hypothesisId": "H4", "location": "compliance_document.py:_compute_critical_empty_fields", "message": "entry", "data": {"ids": self.ids}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        for rec in self:
            missing = []
            if rec.partner_id:
                if not (rec.partner_id.occupation or '').strip():
                    missing.append(_("Ocupación del contacto"))
                if not (rec.partner_id.origin_funds or '').strip():
                    missing.append(_("Origen de fondos (contacto)"))
                if not rec.partner_id.nationality_country_id:
                    missing.append(_("Nacionalidad"))
            if (rec.transaccional_volume or 0) == 0:
                missing.append(_("Volumen transaccional"))
            if missing:
                rec.critical_empty_fields = '<ul>' + ''.join('<li>%s</li>' % m for m in missing) + '</ul>'
            else:
                rec.critical_empty_fields = False

    def action_extract_ocr(self):
        """Extrae texto de adjuntos de la evaluación usando Gemini (OCR)."""
        api_key = (self.env["ir.config_parameter"].sudo().get_param("ghr_compliance.gemini_api_key") or "").strip()
        if not api_key:
            return {"type": "ir.actions.client", "tag": "display_notification", "params": {
                "title": _("Configuración"),
                "message": _("Configure la API key de Gemini en Ajustes para usar OCR."),
                "type": "warning",
                "sticky": False,
            }}
        allowed_mimes = ("image/jpeg", "image/png", "image/gif", "image/webp")
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "compliance.assessment"),
            ("res_id", "=", self.id),
            ("mimetype", "in", allowed_mimes),
        ])
        if not attachments:
            return {"type": "ir.actions.client", "tag": "display_notification", "params": {
                "title": _("Sin adjuntos"),
                "message": _("Suba imágenes (JPEG, PNG, etc.) en el chatter y vuelva a intentar."),
                "type": "warning",
                "sticky": False,
            }}
        created = 0
        for att in attachments:
            existing = self.env["compliance.document.analysis"].search([
                ("attachment_id", "=", att.id),
                ("assessment_id", "=", self.id),
            ], limit=1)
            if existing:
                continue
            if not att.datas:
                continue
            text, err = self.env["compliance.document.analysis"]._call_gemini_ocr(
                api_key, att.datas, getattr(att, "mimetype", None) or "image/jpeg"
            )
            self.env["compliance.document.analysis"].create({
                "attachment_id": att.id,
                "assessment_id": self.id,
                "partner_id": self.partner_id.id,
                "ocr_text": text or ("[Error: %s]" % err if err else ""),
            })
            created += 1
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("OCR completado"),
            "message": _("Se procesaron %s documento(s).") % created,
            "type": "success",
            "sticky": False,
        }}

    def _build_assessment_data_for_prompt(self):
        """Incluye texto OCR de documentos en el prompt para deep search."""
        result = super()._build_assessment_data_for_prompt()
        if not result:
            result = ""
        doc_texts = self.env["compliance.document.analysis"].search([
            ("assessment_id", "=", self.id),
            ("ocr_text", "!=", False),
        ]).mapped("ocr_text")
        if doc_texts:
            result += "\n\n" + _("TEXTO EXTRAÍDO DE DOCUMENTOS ADJUNTOS (OCR):\n") + "\n---\n".join(doc_texts)
        return result
