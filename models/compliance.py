import html
import json
import logging
import os
import re
import urllib.request
import urllib.error

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

# #region agent log
def _debug_log(session_id, hypothesis_id, location, message, data=None):
    try:
        log_path = os.path.join(os.path.dirname(__file__), "..", "debug-076c93.log")
        with open(log_path, "a", encoding="utf-8") as f:
            import time
            f.write(json.dumps({"sessionId": session_id, "hypothesisId": hypothesis_id, "location": location, "message": message, "data": data or {}, "timestamp": int(time.time() * 1000)}) + "\n")
    except Exception:
        pass
# #endregion
from odoo.exceptions import UserError

class ComplianceAssessment(models.Model):
    _name = 'compliance.assessment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Evaluación de Cumplimiento'

    name = fields.Char(string="Código de Evaluación", default="Nuevo", copy=False)
    partner_id = fields.Many2one('res.partner', string="Cliente", required=True, tracking=True)
    profile_id = fields.Many2one(
        'compliance.risk.profile',
        string="Perfil de umbrales",
        help="Define límites de volumen y semáforo (ej. Constructora 400k USD). Editable por el operador.",
        tracking=True,
    )
    form_type = fields.Selection([
        ('inmobiliario_pf', 'Inmobiliario Persona Física'),
        ('inmobiliario_pj', 'Inmobiliario Persona Jurídica'),
        ('automotriz_pf', 'Automotriz (Dealer) Persona Física'),
        ('automotriz_pj', 'Automotriz (Dealer) Persona Jurídica'),
    ], string="Tipo de formulario KYC", default='automotriz_pf', tracking=True)
    survey_id = fields.Many2one(
        'survey.survey',
        string="Encuesta asociada",
        help="Encuesta de debida diligencia usada para esta evaluación.",
    )
    compliance_officer_id = fields.Many2one(
        'res.users',
        string="Oficial de cumplimiento",
        help="Usuario a notificar cuando se complete la encuesta.",
    )
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('ia_process', 'Investigación IA'),
        ('review', 'Revisión Oficial'),
        ('approved', 'Aprobado'),
        ('blocked', 'Rojo Candela (Bloqueado)')
    ], default='draft', string="Estado", tracking=True)

    # Variables de Volumen y Pitufeo
    transaccional_volume = fields.Float(string="Volumen Mensual (USD)", tracking=True)
    transfer_count = fields.Integer(string="Cantidad de Transferencias (Inicial)", default=1, tracking=True)

    # Variables del Cuentagotas (Puntos del 1 al 10)
    origin_funds_score = fields.Integer(string="Puntos: Origen de Fondos (1-10)", default=1) 
    economic_activity_score = fields.Integer(string="Puntos: Actividad Economica (1-10)", default=1)   
    pep_score = fields.Integer(string="Puntos: PEP (0-10)", default=0)                     
    nationality_score = fields.Integer(string="Puntos: Nacionalidad (1-10)", default=1)    
    transaction_bulto_score = fields.Integer(string="Puntos: Transacción Bulto (1-10)", default=1)   
    
    # IA y Castigo
    ai_penalty = fields.Integer(string="Castigo IA (Macos)", default=0) 
    ai_report = fields.Html(string="Resumen del Chivatazo IA")

    # Resultado Final Computado
    risk_score = fields.Float(string="Score Total (0-100)", compute="_compute_total_risk", store=True)
    risk_level = fields.Selection([
        ('low', 'Bajo (Verde)'),
        ('medium', 'Medio (Amarillo)'),
        ('high', 'Alto (Rojo Candela)')
    ], string="Nivel de Riesgo", compute="_compute_total_risk", store=True)
    risk_alert_message = fields.Html(
        string="Alertas activas",
        compute="_compute_risk_alert_message",
        sanitize=False,
    )
    purchase_capacity_monthly = fields.Float(
        string="Capacidad de adquisición (mensual)",
        compute="_compute_purchase_capacity_monthly",
        store=True,
        help="Salario + otros ingresos del contacto (mensual).",
    )

    @api.depends('partner_id', 'partner_id.monthly_salary', 'partner_id.other_income')
    def _compute_purchase_capacity_monthly(self):
        for rec in self:
            if rec.partner_id:
                rec.purchase_capacity_monthly = (rec.partner_id.monthly_salary or 0) + (rec.partner_id.other_income or 0)
            else:
                rec.purchase_capacity_monthly = 0.0

    def _get_thresholds(self):
        """Devuelve umbrales y pesos del perfil asignado o por defecto (para score, semáforo y alertas)."""
        self.ensure_one()
        # #region agent log
        _debug_log("076c93", "H1", "compliance.py:_get_thresholds", "entry", {"rec_id": self.id, "profile_id": self.profile_id.id if self.profile_id else None})
        # #endregion
        if self.profile_id:
            return {
                'volume_limit': self.profile_id.volume_limit_usd,
                'transfer_limit': self.profile_id.transfer_count_limit,
                'score_medium': self.profile_id.score_threshold_medium,
                'score_high': self.profile_id.score_threshold_high,
                'w_origin': self.profile_id.weight_origin_funds,
                'w_activity': self.profile_id.weight_economic_activity,
                'w_pep': self.profile_id.weight_pep,
                'w_geo': self.profile_id.weight_nationality,
                'w_volume': self.profile_id.weight_transaction_bulto,
            }
        return {
            'volume_limit': 15000.0,
            'transfer_limit': 3,
            'score_medium': 36,
            'score_high': 71,
            'w_origin': 0.30,
            'w_activity': 0.25,
            'w_pep': 0.20,
            'w_geo': 0.15,
            'w_volume': 0.10,
        }

    @api.depends('origin_funds_score', 'economic_activity_score', 'pep_score', 
                 'nationality_score', 'transaction_bulto_score', 'ai_penalty', 'profile_id')
    def _compute_total_risk(self):
        # #region agent log
        _debug_log("076c93", "H1", "compliance.py:_compute_total_risk", "entry", {"ids": self.ids})
        # #endregion
        for rec in self:
            th = rec._get_thresholds()
            # Cálculo ponderado con pesos configurables por perfil
            base_score = (
                (rec.origin_funds_score * th['w_origin']) +
                (rec.economic_activity_score * th['w_activity']) +
                (rec.pep_score * th['w_pep']) +
                (rec.nationality_score * th['w_geo']) +
                (rec.transaction_bulto_score * th['w_volume'])
            ) * 10 

            # Ajustes adicionales por parámetros de riesgo (país, banco, cliente)
            parameter_penalty = 0.0
            partner = rec.partner_id
            if partner:
                RiskParam = rec.env['compliance.risk.parameter'].sudo()
                # Por país de nacionalidad
                if partner.country_id:
                    params_country = RiskParam.search([
                        ('parameter_type', '=', 'country'),
                        ('country_id', '=', partner.country_id.id),
                        ('active', '=', True),
                    ])
                    if params_country:
                        parameter_penalty += max(params_country.mapped('base_score')) * 10
                # Por banco asociado (bank_ids son res.partner.bank; necesitamos los res.bank)
                if hasattr(partner, 'bank_ids') and partner.bank_ids:
                    bank_ids = partner.bank_ids.mapped('bank_id').filtered(lambda b: b).ids
                    if bank_ids:
                        params_bank = RiskParam.search([
                            ('parameter_type', '=', 'bank'),
                            ('bank_id', 'in', bank_ids),
                            ('active', '=', True),
                        ])
                        if params_bank:
                            parameter_penalty += max(params_bank.mapped('base_score')) * 10
                # Parámetro directo por cliente
                params_partner = RiskParam.search([
                    ('parameter_type', '=', 'partner'),
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                ])
                if params_partner:
                    parameter_penalty += max(params_partner.mapped('base_score')) * 10

            total = base_score + rec.ai_penalty + parameter_penalty
            rec.risk_score = total

            if total >= th['score_high']:
                rec.risk_level = 'high'
                if rec.state != 'approved': rec.state = 'blocked'
            elif total >= th['score_medium']:
                rec.risk_level = 'medium'
            else:
                rec.risk_level = 'low'

    @api.depends('transaccional_volume', 'origin_funds_score', 'transfer_count', 'profile_id',
                 'purchase_capacity_monthly')
    def _compute_risk_alert_message(self):
        """Alertas visibles en ficha según umbrales del perfil (aunque los campos estén en solo lectura)."""
        # #region agent log
        _debug_log("076c93", "H4", "compliance.py:_compute_risk_alert_message", "entry", {"ids": self.ids})
        # #endregion
        for rec in self:
            th = rec._get_thresholds()
            limit = th['volume_limit']
            transfer_limit = th['transfer_limit']
            volume = rec.transaccional_volume or 0.0
            items = []  # list of (css_border, text)

            if volume >= limit and rec.origin_funds_score == 10:
                items.append(('#c0392b', _("Este cliente supera USD %s con Origen de Fondos en 10 (sospechoso). Posible lavado.") % int(limit)))
            elif volume >= limit:
                items.append(('#e67e22', _("Volumen mensual (USD %s) supera el límite de este perfil (USD %s).") % (int(volume), int(limit))))

            if (rec.transfer_count or 0) > transfer_limit:
                items.append(('#8e44ad', _("Más de %s transferencias para el inicial. Posible fraccionamiento de fondos.") % transfer_limit))

            # Alarma inconsistencia: monto muy superior a capacidad declarada
            capacity = rec.purchase_capacity_monthly or 0.0
            if capacity > 0 and volume > 0 and volume > 3 * capacity:
                items.append(('#c0392b', _("Monto desproporcionado respecto a capacidad declarada: volumen USD %s vs capacidad mensual USD %s.") % (int(volume), int(capacity))))

            if items:
                blocks = [
                    '<div style="flex:0 1 auto;min-width:220px;max-width:400px;padding:0.5rem 0.75rem;margin-right:0.5rem;border-left:4px solid %s;background:#f8f9fa;border-radius:4px;font-size:13px;line-height:1.4;writing-mode:horizontal-tb;text-align:left;word-wrap:break-word;">%s</div>' % (color, text)
                    for color, text in items
                ]
                if len(items) > 1:
                    blocks[-1] = blocks[-1].replace('margin-right:0.5rem', 'margin-right:0')
                rec.risk_alert_message = '<div style="margin:0;padding:0;display:flex;flex-direction:row;flex-wrap:wrap;gap:0.5rem;align-items:stretch;">' + ''.join(blocks) + '</div>'
            else:
                rec.risk_alert_message = False

    # 🔥 ALERTAS DE RIESGO (usan umbrales del perfil asignado) — también popup al editar
    @api.onchange('transaccional_volume', 'origin_funds_score', 'transfer_count', 'profile_id', 'purchase_capacity_monthly')
    def _onchange_risk_alerts(self):
        # #region agent log
        _debug_log("076c93", "H5", "compliance.py:_onchange_risk_alerts", "entry", {"rec_id": getattr(self, "id", None), "origin": getattr(self, "origin_funds_score", None)})
        # #endregion
        th = self._get_thresholds()
        limit = th['volume_limit']
        transfer_limit = th['transfer_limit']
        volume = self.transaccional_volume or 0.0
        messages = []

        if volume >= limit and self.origin_funds_score == 10:
            messages.append(_("🔥 ALERTA ROJA: Este cliente supera USD %s con Origen de Fondos en 10 (sospechoso). Posible lavado.") % int(limit))
        elif volume >= limit:
            messages.append(_("⚠️ Volumen mensual (USD %s) supera el límite de este perfil (USD %s).") % (int(volume), int(limit)))

        if (self.transfer_count or 0) > transfer_limit:
            messages.append(_("🚩 ALERTA DE PITUFEO: Más de %s transferencias para el inicial. Posible fraccionamiento de fondos.") % transfer_limit)

        capacity = self.purchase_capacity_monthly or 0.0
        if capacity > 0 and volume > 0 and volume > 3 * capacity:
            messages.append(_("⚠️ Monto desproporcionado respecto a capacidad declarada: volumen USD %s vs capacidad mensual USD %s.") % (int(volume), int(capacity)))

        if messages:
            return {
                'warning': {
                    'title': _("Alertas de riesgo"),
                    'message': '\n\n'.join(messages),
                }
            }

    # Prompt para Gemini: Oficial de Cumplimiento de Autrab (dictamen ultracompacto, 3 bloques)
    _GEMINI_DICTAMEN_PROMPT = """Actúa como Oficial de Cumplimiento de Autrab. Realiza un análisis forense de los datos suministrados para la importación del vehículo y genera un dictamen ultracompacto (máximo 15 líneas).

Tu análisis debe estar estructurado en solo 3 bloques de texto corrido:

BLOQUE 1: COHERENCIA Y RIESGO (Texto continuo)
Evalúa en un solo párrafo denso:
1. Si la identidad y residencia son consistentes.
2. Si el "Perfil Económico" (Ingresos/Cargo) justifica lógicamente el "Nivel de Operaciones" y la importación (ej. ¿Tiene capacidad real de compra y mantenimiento?).
3. Si el "Origen de Fondos" es específico o vago.
4. Si es PEP o tiene riesgo reputacional alto según tu base de conocimiento interna.

BLOQUE 2: CALIDAD DEL ENTORNO (Texto continuo)
Evalúa brevemente si las Referencias Comerciales y Bancarias denotan formalidad (correos corporativos, bancos reconocidos) o informalidad riesgosa.

BLOQUE 3: BÚSQUEDA Y VEREDICTO
Provee 3 cadenas de búsqueda exactas (Google Dorks) separadas por " | " (barras verticales) para ahorrar espacio, y termina con una frase de CONCLUSIÓN FINAL (Aprobado/Revisar/Rechazar).

---
FORMATO DE SALIDA REQUERIDO:
**RIESGO DETECTADO:** [BAJO/MEDIO/ALTO]

**ANÁLISIS INTEGRAL:** [Aquí redacta el Bloque 1 y 2 unidos en un solo párrafo potente y directo sin saltos de línea innecesarios].

**BÚSQUEDAS SUGERIDAS:** `[Cadena 1]` | `[Cadena 2]` | `[Cadena 3]`

**DICTAMEN:** [Frase final corta].
"""

    def _build_assessment_data_for_prompt(self):
        """Devuelve un texto con los datos de la evaluación para inyectar en el prompt de Gemini."""
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            return ""
        nombre = partner.name or _("Cliente sin nombre")
        pais = partner.country_id.name if partner.country_id else _("No declarado")
        riesgo_map = {'low': 'BAJO', 'medium': 'MEDIO', 'high': 'ALTO'}
        riesgo = riesgo_map.get(self.risk_level or 'low')
        lines = [
            _("DATOS DE LA EVALUACIÓN (importación vehículo):"),
            _("- Cliente: %s") % nombre,
            _("- País/Residencia: %s") % pais,
            _("- Puntos Origen de Fondos (1-10): %s") % self.origin_funds_score,
            _("- Puntos Actividad Económica (1-10): %s") % self.economic_activity_score,
            _("- Puntos PEP (0-10): %s") % self.pep_score,
            _("- Puntos Nacionalidad (1-10): %s") % self.nationality_score,
            _("- Puntos Transacción Bulto (1-10): %s") % self.transaction_bulto_score,
            _("- Volumen mensual declarado (USD): %s") % (int(self.transaccional_volume) if self.transaccional_volume else _("No declarado")),
            _("- Cantidad de transferencias (inicial): %s") % (self.transfer_count or 0),
            _("- Nivel de riesgo calculado: %s") % riesgo,
            _("- Score total (0-100): %s") % (round(self.risk_score, 1) if self.risk_score else 0),
        ]
        return "\n".join(lines)

    def _call_gemini_api(self, api_key, prompt_text):
        """Llama a la API de Gemini y devuelve el texto generado o None si hay error."""
        # Usar modelo estable: gemini-2.0-flash o gemini-1.5-flash-latest evita 404 con v1beta
        model_name = "gemini-2.0-flash"
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model_name
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        body = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "maxOutputTokens": 1024,
                "temperature": 0.3,
            },
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _logger.warning("ghr_compliance Gemini API HTTP error: %s", e, exc_info=True)
            if e.code == 404:
                return None, _("Modelo no disponible (404). Verifique la API key y el nombre del modelo en Ajustes.")
            return None, _("Error HTTP %s: %s") % (e.code, e.reason or "")
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            _logger.warning("ghr_compliance Gemini API error: %s", e, exc_info=True)
            return None, _("Error de conexión o respuesta inválida.")
        candidates = data.get("candidates") or []
        if not candidates:
            return None, _("La API no devolvió respuesta.")
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return None, _("Respuesta vacía.")
        text = (parts[0].get("text") or "").strip()
        if not text:
            return None, _("Texto vacío.")
        return text, None

    def _dictamen_text_to_html(self, text):
        """Convierte el texto del dictamen (con **bold** y saltos) a HTML seguro."""
        if not text:
            return ""
        text = html.escape(text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        lines = text.split("\n")
        out = []
        for line in lines:
            line = line.strip()
            if line:
                out.append("<p>%s</p>" % line)
        return "".join(out) if out else "<p>%s</p>" % text

    def _build_fallback_ai_report(self):
        """Genera el dictamen por reglas internas (sin IA)."""
        self.ensure_one()
        if not self.partner_id:
            return ""
        partner = self.partner_id
        nombre = partner.name or _("Cliente sin nombre")
        pais = partner.country_id.name if partner.country_id else ""
        riesgo_map = {'low': _("BAJO"), 'medium': _("MEDIO"), 'high': _("ALTO")}
        riesgo_detectado = riesgo_map.get(self.risk_level or 'low')

        capacidad = _("coherente") if (self.origin_funds_score >= 4 and self.economic_activity_score >= 4) else _("ajustada")
        if self.origin_funds_score <= 2 or self.economic_activity_score <= 2:
            capacidad = _("limitada")

        if self.origin_funds_score >= 7:
            origen = _("percibido como de alto riesgo y poco justificable")
        elif self.origin_funds_score >= 4:
            origen = _("moderadamente sensible pero parcialmente justificable")
        else:
            origen = _("razonablemente específico y trazable según la encuesta")

        if self.pep_score and self.pep_score >= 7:
            reputacion = _("con indicios de exposición política o reputacional relevante")
        elif self.pep_score and self.pep_score >= 4:
            reputacion = _("con cierta exposición política que amerita seguimiento")
        else:
            reputacion = _("sin señales claras de PEP ni riesgos reputacionales evidentes en la información disponible")

        volumen_txt = _("sin volumen mensual declarado")
        if self.transaccional_volume:
            volumen_txt = _("con volumen mensual estimado de USD %s") % int(self.transaccional_volume)
        transfer_txt = _("y número de transferencias iniciales %s") % int(self.transfer_count or 0)
        entorno = _("las referencias comerciales y bancarias registradas en el expediente se consideran neutrales, sin evidencia externa de formalidad ni señales de alerta adicionales dentro del sistema")

        analisis_integral = _(
            "La identidad y residencia de %(nombre)s en %(pais)s resultan consistentes con la información disponible, "
            "su perfil económico (puntuaciones de origen de fondos y actividad económica) se percibe %(capacidad)s para sostener el nivel de operaciones declarado %(volumen)s %(transfer)s, "
            "el origen de fondos se valora como %(origen)s y el perfil político/reputacional se aprecia %(reputacion)s; en conjunto, %(entorno)s."
        ) % {
            'nombre': nombre,
            'pais': pais or _("el país declarado"),
            'capacidad': capacidad,
            'volumen': volumen_txt,
            'transfer': transfer_txt,
            'origen': origen,
            'reputacion': reputacion,
            'entorno': entorno,
        }

        safe_name = nombre.replace('"', '')
        base = '"%s"' % safe_name
        q1 = ("%s %s fraude" % (base, pais or "")).strip()
        q2 = '%s "lavado de dinero"' % base
        q3 = "%s vehiculo lujo sanciones" % base

        if self.risk_level == 'low':
            dictamen = _("Aprobado")
        elif self.risk_level == 'medium':
            dictamen = _("Revisar")
        else:
            dictamen = _("Revisar")

        return (
            "<p><b>RIESGO DETECTADO:</b> %s</p>"
            "<p><b>ANÁLISIS INTEGRAL:</b> %s</p>"
            "<p><b>BÚSQUEDAS SUGERIDAS:</b> `%s` | `%s` | `%s`</p>"
            "<p><b>DICTAMEN:</b> %s.</p>"
        ) % (riesgo_detectado, analisis_integral, q1, q2, q3, dictamen)

    def action_generate_ai_report(self):
        """Genera el dictamen y lo guarda en ai_report. Usa Gemini si hay API key; si no, dictamen por reglas."""
        for rec in self:
            if not rec.partner_id:
                continue

            api_key = (rec.env["ir.config_parameter"].sudo().get_param("ghr_compliance.gemini_api_key") or "").strip()

            if api_key:
                data_block = rec._build_assessment_data_for_prompt()
                full_prompt = "%s\n\n%s" % (data_block, rec._GEMINI_DICTAMEN_PROMPT)
                text, err = rec._call_gemini_api(api_key, full_prompt)
                if text and not err:
                    rec.ai_report = rec._dictamen_text_to_html(text)
                    continue
                fallback_html = rec._build_fallback_ai_report()
                notice = _(
                    "<p><i>No se pudo generar el dictamen con IA (error o sin respuesta). "
                    "Se muestra el dictamen por reglas internas. Verifique la API key en Ajustes o la conexión.</i></p>"
                )
                rec.ai_report = notice + fallback_html
            else:
                rec.ai_report = rec._build_fallback_ai_report()

    def action_approve(self):
        """ Solo un administrador puede aprobar si el riesgo es Alto """
        if self.risk_level == 'high' and not self.env.user.has_group('base.group_system'):
            raise UserError(_("¡No hay tutía! Este cliente es Rojo Candela y solo puede ser aprobado por la gerencia."))
        self.state = 'approved'

    def action_send_survey_invitation(self):
        """Crea respuesta de encuesta, genera enlace y envía email al contacto."""
        self.ensure_one()
        if not self.survey_id:
            raise UserError(_("Asigne una encuesta a esta evaluación antes de enviar la invitación."))
        if not self.partner_id or not self.partner_id.email:
            raise UserError(_("El contacto debe tener email para enviar la invitación."))
        survey = self.survey_id
        user_inputs = survey._create_answer(partner=self.partner_id, check_attempts=False)
        if not user_inputs:
            raise UserError(_("No se pudo crear la respuesta de encuesta."))
        user_input = user_inputs[0]
        base_url = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        start_path = survey.get_start_url()
        fill_url = "%s%s?answer_token=%s" % (base_url, start_path, user_input.access_token)
        subject = _("Invitación: %s") % survey.title
        body = _(
            "<p>Estimado/a,</p><p>Le invitamos a completar la encuesta de debida diligencia.</p>"
            "<p><a href=\"%s\">Acceder a la encuesta</a></p><p>Enlace directo: %s</p>"
        ) % (fill_url, fill_url)
        self.env["mail.mail"].sudo().create({
            "model": "compliance.assessment",
            "res_id": self.id,
            "email_to": self.partner_id.email,
            "subject": subject,
            "body_html": body,
        }).send()
        self.message_post(body=_("Invitación a encuesta enviada por email a %s.") % self.partner_id.email)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Enviado"),
            "message": _("Invitación enviada a %s.") % self.partner_id.email,
            "type": "success",
            "sticky": False,
        }}

    @api.model
    def create(self, vals):
        if vals.get('name', 'Nuevo') == 'Nuevo':
            vals['name'] = self.env['ir.sequence'].next_by_code('compliance.assessment') or 'Nuevo'
        return super(ComplianceAssessment, self).create(vals)

    @api.model
    def _ensure_res_partner_columns(self):
        """Migración: añade columnas de cumplimiento a res_partner si faltan (al actualizar módulo)."""
        from ..hooks import _add_res_partner_columns
        _add_res_partner_columns(self.env.cr)

    @api.model
    def _cron_remind_pending_assessments(self):
        """Envía recordatorio por email para evaluaciones en borrador antiguas sin encuesta completada."""
        from datetime import datetime, timedelta
        days = int(self.env["ir.config_parameter"].sudo().get_param("ghr_compliance.reminder_days", "7"))
        limit_date = datetime.now() - timedelta(days=days)
        assessments = self.search([
            ("state", "=", "draft"),
            ("create_date", "<", limit_date),
            ("partner_id.email", "!=", False),
        ])
        for assessment in assessments:
            if not assessment.survey_id:
                continue
            body = _(
                "<p>Estimado/a,</p><p>Le recordamos que tiene pendiente completar la encuesta de debida diligencia "
                "para la evaluación %s.</p><p>Por favor, complete la encuesta a la mayor brevedad.</p>"
            ) % assessment.name
            self.env["mail.mail"].sudo().create({
                "model": "compliance.assessment",
                "res_id": assessment.id,
                "email_to": assessment.partner_id.email,
                "subject": _("Recordatorio: Encuesta de cumplimiento pendiente"),
                "body_html": body,
            }).send()

# --- Perfiles de riesgo: umbrales personalizables por el cliente/operador desde la UI ---
class ComplianceRiskProfile(models.Model):
    _name = 'compliance.risk.profile'
    _description = 'Perfil de umbrales de riesgo (ej. Constructora, Dealer)'

    name = fields.Char(string="Nombre del perfil", required=True, help="Ej: Constructora, Dealer, Default")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string="Compañía", help="Vacío = aplica a todas")
    # Umbral de volumen mensual (USD): por encima de este valor se disparan alertas
    volume_limit_usd = fields.Float(string="Límite volumen mensual (USD)", default=15000.0)
    # Máximo de transferencias para alerta de pitufeo
    transfer_count_limit = fields.Integer(string="Límite transferencias (alerta pitufeo)", default=3)
    # Score mínimo para considerar riesgo Medio (semáforo amarillo)
    score_threshold_medium = fields.Integer(string="Score mínimo Riesgo Medio", default=36)
    # Score mínimo para considerar riesgo Alto (semáforo rojo)
    score_threshold_high = fields.Integer(string="Score mínimo Riesgo Alto", default=71)

    # Pesos de las variables en el score (suma ideal 1.0, pero el usuario puede ajustarlos)
    weight_origin_funds = fields.Float(string="Peso Origen de Fondos", default=0.30)
    weight_economic_activity = fields.Float(string="Peso Actividad Económica", default=0.25)
    weight_pep = fields.Float(string="Peso PEP", default=0.20)
    weight_nationality = fields.Float(string="Peso Nacionalidad", default=0.15)
    weight_transaction_bulto = fields.Float(string="Peso Transacción Bulto", default=0.10)

    _sql_constraints = [
        ('name_company_uniq', 'unique(name, company_id)', 'Ya existe un perfil con este nombre en la compañía.'),
    ]


class ComplianceRiskParameter(models.Model):
    _name = 'compliance.risk.parameter'
    _description = 'Parámetro de riesgo por país, banco o cliente'

    name = fields.Char(string="Descripción", required=True)
    active = fields.Boolean(default=True)
    parameter_type = fields.Selection([
        ('country', 'País / Nacionalidad'),
        ('bank', 'Banco'),
        ('partner', 'Cliente específico'),
    ], string="Tipo de parámetro", required=True, default='country')

    country_id = fields.Many2one('res.country', string="País")
    bank_id = fields.Many2one('res.bank', string="Banco")
    partner_id = fields.Many2one('res.partner', string="Cliente")

    base_score = fields.Integer(
        string="Puntos de riesgo (0-10)",
        default=5,
        help="Puntos adicionales que se suman al score (se multiplican por 10 internamente)."
    )

    _sql_constraints = [
        ('country_unique', 'unique(parameter_type, country_id)',
         'Ya existe un parámetro de riesgo para este país.'),
        ('bank_unique', 'unique(parameter_type, bank_id)',
         'Ya existe un parámetro de riesgo para este banco.'),
        ('partner_unique', 'unique(parameter_type, partner_id)',
         'Ya existe un parámetro de riesgo para este cliente.'),
    ]


# --- MODELO COMPLEMENTARIO PARA EL XML ---
class ComplianceQuestion(models.Model):
    _name = 'compliance.question'
    _description = 'Preguntas de la Matriz de Riesgo'
    _order = 'sequence, id'

    name = fields.Char(string="Pregunta", required=True)
    sequence = fields.Integer(string="Secuencia", default=10)
    category = fields.Selection([
        ('dealer', 'Dealer'),
        ('constructora', 'Constructora'),
        ('ambos', 'Ambos')
    ], string="Categoría (legacy)", default='ambos', help="Compatibilidad con data existente.")
    form_type = fields.Selection([
        ('inmobiliario_pf', 'Inmobiliario Persona Física'),
        ('inmobiliario_pj', 'Inmobiliario Persona Jurídica'),
        ('automotriz_pf', 'Automotriz (Dealer) Persona Física'),
        ('automotriz_pj', 'Automotriz (Dealer) Persona Jurídica'),
        ('ambos', 'Todos los formularios'),
    ], string="Tipo de formulario", default='ambos')
    weight = fields.Integer(string="Puntos (1-10)", default=1)
    is_scorable = fields.Boolean(string="Calificable", default=True, help="Si está marcado, la respuesta incide en el score.")
    destination_field = fields.Char(
        string="Campo destino",
        help="Nombre del campo en compliance.assessment o res.partner al que mapear (ej. origin_funds_score, occupation)."
    )


# Opciones amigables para "dónde guardar la respuesta" (sin jerga técnica)
DESTINATION_MAPPING_OPTIONS = [
    ('assessment,origin_funds_score', 'Evaluación: Puntaje origen de fondos'),
    ('assessment,economic_activity_score', 'Evaluación: Puntaje actividad económica'),
    ('assessment,pep_score', 'Evaluación: Puntaje PEP'),
    ('assessment,nationality_score', 'Evaluación: Puntaje nacionalidad'),
    ('assessment,transaction_bulto_score', 'Evaluación: Puntaje transacción bulto'),
    ('assessment,transaccional_volume', 'Evaluación: Volumen mensual (USD)'),
    ('assessment,transfer_count', 'Evaluación: Cantidad de transferencias'),
    ('partner,occupation', 'Contacto: Ocupación / Cargo'),
    ('partner,origin_funds', 'Contacto: Origen de fondos'),
    ('partner,is_pep', 'Contacto: Es PEP'),
    ('partner,nationality_country_id', 'Contacto: Nacionalidad'),
    ('partner,monthly_salary', 'Contacto: Salario mensual'),
    ('partner,other_income', 'Contacto: Otros ingresos mensuales'),
    ('partner,compliance_notes', 'Contacto: Notas de cumplimiento'),
]


class ComplianceQuestionMapping(models.Model):
    _name = 'compliance.question.mapping'
    _description = 'Dónde se guarda cada respuesta de la encuesta'

    survey_question_title = fields.Char(
        string="Texto que debe tener el título de la pregunta",
        required=True,
        help="Palabra o frase que debe aparecer en el título de la pregunta en la encuesta. Cuando el cliente responda, su respuesta se guardará en el destino elegido abajo.",
    )
    form_type = fields.Selection([
        ('inmobiliario_pf', 'Inmobiliario Persona Física'),
        ('inmobiliario_pj', 'Inmobiliario Persona Jurídica'),
        ('automotriz_pf', 'Automotriz (Dealer) Persona Física'),
        ('automotriz_pj', 'Automotriz (Dealer) Persona Jurídica'),
        ('ambos', 'Todos'),
    ], string="Tipo de formulario", default='ambos', required=True,
       help="Para qué tipo de evaluación aplica esta regla.")
    destination_field = fields.Char(required=True, default='origin_funds_score')  # Uso interno
    destination_model = fields.Selection([
        ('assessment', 'assessment'),
        ('partner', 'partner'),
    ], default='assessment', required=True)  # Uso interno
    destination_key = fields.Selection(
        selection=DESTINATION_MAPPING_OPTIONS,
        string="Guardar respuesta en",
        compute="_compute_destination_key",
        inverse="_inverse_destination_key",
        help="Elija dónde debe guardarse la respuesta cuando el título de la pregunta coincida.",
    )

    @api.depends('destination_model', 'destination_field')
    def _compute_destination_key(self):
        for rec in self:
            if rec.destination_model and rec.destination_field:
                key = '%s,%s' % (rec.destination_model, rec.destination_field)
                rec.destination_key = key if any(k[0] == key for k in DESTINATION_MAPPING_OPTIONS) else False
            else:
                rec.destination_key = False

    def _inverse_destination_key(self):
        for rec in self:
            if rec.destination_key:
                parts = rec.destination_key.split(',', 1)
                if len(parts) == 2:
                    rec.destination_model = parts[0]
                    rec.destination_field = parts[1]