# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class ComplianceApiConfig(models.TransientModel):
    _name = 'compliance.api.config'
    _description = 'Configuración de claves API para Compliance (OCR y dictamen)'

    openai_api_key = fields.Char(
        string='API Key OpenAI (GPT)',
        help='Clave API de OpenAI para OCR de documentos y dictamen con IA. '
             'Obtenerla en: https://platform.openai.com/api-keys',
        password=True,
    )
    gemini_api_key = fields.Char(
        string='API Key Gemini (Google AI)',
        help='Clave API de Google AI Studio. Se usa si no hay clave de OpenAI. '
             'Obtenerla en: https://aistudio.google.com/apikey',
        password=True,
    )
    ia_cost_per_call = fields.Float(
        string='Costo por llamada IA (créditos)',
        default=1.0,
        help='Créditos consumidos por cada llamada de IA (por imagen/página procesada).',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        params = self.env['ir.config_parameter'].sudo()
        if 'openai_api_key' in fields_list:
            res['openai_api_key'] = (params.get_param('ghr_compliance.openai_api_key') or '').strip()
        if 'gemini_api_key' in fields_list:
            res['gemini_api_key'] = (params.get_param('ghr_compliance.gemini_api_key') or '').strip()
        if 'ia_cost_per_call' in fields_list:
            raw = params.get_param('ghr_compliance.ia_cost_per_call')
            res['ia_cost_per_call'] = float(raw) if raw is not None else 1.0
        return res

    def action_save(self):
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ghr_compliance.openai_api_key', (self.openai_api_key or '').strip())
        params.set_param('ghr_compliance.gemini_api_key', (self.gemini_api_key or '').strip())
        params.set_param('ghr_compliance.ia_cost_per_call', self.ia_cost_per_call or 1.0)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Guardado'),
                'message': _('Claves API actualizadas. Se usarán para OCR y dictamen con IA.'),
                'type': 'success',
                'sticky': False,
            },
        }
