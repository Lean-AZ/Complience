# -*- coding: utf-8 -*-
from odoo import models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Las claves API se configuran en Compliance > Configuración > Claves API
