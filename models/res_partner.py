# -*- coding: utf-8 -*-
import json
import os
import time
from odoo import models, fields, api, _


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _register_hook(self):
        """Al cargar el modelo: crea columnas en res_partner si faltan (sin desinstalar)."""
        super()._register_hook()
        try:
            done = self.env["ir.config_parameter"].sudo().get_param("ghr_compliance.res_partner_columns_done")
            if done != "1":
                from ..hooks import _add_res_partner_columns
                _add_res_partner_columns(self.env.cr)
                self.env["ir.config_parameter"].sudo().set_param("ghr_compliance.res_partner_columns_done", "1")
        except Exception:
            pass

    # KYC / PLAFT
    occupation = fields.Char(string="Ocupación / Cargo")
    origin_funds = fields.Text(string="Origen de fondos (resumen)")
    is_pep = fields.Boolean(string="Es PEP")
    nationality_country_id = fields.Many2one('res.country', string="Nacionalidad")
    monthly_salary = fields.Float(string="Salario mensual")
    other_income = fields.Float(string="Otros ingresos mensuales")
    compliance_notes = fields.Text(string="Notas de cumplimiento")

    compliance_assessment_ids = fields.One2many(
        'compliance.assessment',
        'partner_id',
        string="Evaluaciones PLAFT",
        readonly=True,
    )
    last_compliance_assessment_id = fields.Many2one(
        'compliance.assessment',
        string="Última evaluación",
        compute="_compute_last_compliance_assessment",
        store=True,
        readonly=True,
    )

    @api.depends('compliance_assessment_ids', 'compliance_assessment_ids.create_date')
    def _compute_last_compliance_assessment(self):
        # #region agent log
        try:
            log_path = os.path.join(os.path.dirname(__file__), "..", "debug-076c93.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"sessionId": "076c93", "hypothesisId": "H3", "location": "res_partner.py:_compute_last_compliance_assessment", "message": "entry", "data": {"ids": self.ids}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        for partner in self:
            last = partner.compliance_assessment_ids.sorted('create_date', reverse=True)[:1]
            partner.last_compliance_assessment_id = last if last else False

    def action_new_compliance_assessment(self):
        """Abre el wizard o formulario para crear una nueva evaluación con este contacto."""
        self.ensure_one()
        return {
            'name': _('Nueva evaluación PLAFT'),
            'type': 'ir.actions.act_window',
            'res_model': 'compliance.assessment',
            'view_mode': 'form',
            'target': 'current',
            'context': {'default_partner_id': self.id},
        }
