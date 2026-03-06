# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ghr_compliance_gemini_api_key = fields.Char(
        string='API Key Gemini (Google AI)',
        config_parameter='ghr_compliance.gemini_api_key',
        help='Clave API de Google AI Studio (Gemini) para generar dictámenes de cumplimiento. '
             'Obtenerla en: https://aistudio.google.com/apikey. Dejar vacía para usar solo el dictamen por reglas internas.',
        groups='base.group_system',
    )
