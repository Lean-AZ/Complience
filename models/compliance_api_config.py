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
    ocr_price_per_1k_tokens_usd = fields.Float(
        string='Precio OCR USD / 1K tokens',
        default=0.0001,
        digits=(16, 6),
        help='Costo en USD por cada 1,000 tokens usados en OCR (solo aplica cuando se usa OpenAI GPT). '
             'Como referencia, gpt-4o-mini tiene precios aproximados de 0.0001 USD/1K tokens de entrada y '
             '0.0006 USD/1K tokens de salida; ajuste este valor según su contrato real.',
    )
    dictamen_price_per_1k_tokens_usd = fields.Float(
        string='Precio dictamen USD / 1K tokens',
        default=0.0006,
        digits=(16, 6),
        help='Costo en USD por cada 1,000 tokens usados en el dictamen IA (solo aplica cuando se usa OpenAI GPT). '
             'Como referencia, gpt-4o-mini tiene precios aproximados de 0.0001 USD/1K tokens de entrada y '
             '0.0006 USD/1K tokens de salida; ajuste este valor según su contrato real.',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        params = self.env['ir.config_parameter'].sudo()
        if 'openai_api_key' in fields_list:
            res['openai_api_key'] = (params.get_param('ghr_compliance.openai_api_key') or '').strip()
        if 'gemini_api_key' in fields_list:
            res['gemini_api_key'] = (params.get_param('ghr_compliance.gemini_api_key') or '').strip()
        if 'ocr_price_per_1k_tokens_usd' in fields_list:
            raw_ocr = params.get_param('ghr_compliance.ocr_price_per_1k_tokens_usd')
            res['ocr_price_per_1k_tokens_usd'] = float(raw_ocr) if raw_ocr is not None else 0.0
        if 'dictamen_price_per_1k_tokens_usd' in fields_list:
            raw_dict = params.get_param('ghr_compliance.dictamen_price_per_1k_tokens_usd')
            res['dictamen_price_per_1k_tokens_usd'] = float(raw_dict) if raw_dict is not None else 0.0
        return res

    def action_save(self):
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ghr_compliance.openai_api_key', (self.openai_api_key or '').strip())
        params.set_param('ghr_compliance.gemini_api_key', (self.gemini_api_key or '').strip())
        params.set_param(
            'ghr_compliance.ocr_price_per_1k_tokens_usd',
            self.ocr_price_per_1k_tokens_usd or 0.0,
        )
        params.set_param(
            'ghr_compliance.dictamen_price_per_1k_tokens_usd',
            self.dictamen_price_per_1k_tokens_usd or 0.0,
        )
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
