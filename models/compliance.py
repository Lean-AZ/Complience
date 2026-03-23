import html
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import csv
import base64
import unicodedata

from odoo import models, fields, api, _
from odoo.tools import safe_eval

from .survey_input import _load_kyc_pf_mapping

# Reintentos ante 429 (Too Many Requests) en dictamen
_MAX_RETRIES_429 = 3
_DELAY_429_SECONDS = (2, 5, 10)

_logger = logging.getLogger(__name__)

_PROMPT_PERFIL_OSINT = """
Actúa como especialista en Inteligencia de Fuentes Abiertas (OSINT). Analiza el siguiente perfil para nuestra matriz de riesgo: Cliente [{kyc_nombre}], Nacionalidad [{kyc_nacionalidad}], Ocupación/Empresa [{kyc_ocupacion}].
Aplica un análisis de búsqueda profunda en internet (Deep Search) basado en tu conocimiento para detectar si existen noticias negativas, exposición política (PEP), relación con lavado de activos o presencia en listas de sanciones internacionales para este perfil o industria en su país.
Sintetiza cualquier hallazgo relevante o, si no hay registros, evalúa el riesgo reputacional inherente a su ocupación y procedencia.
Devuelve tu reporte de riesgo y alertas en un máximo de cuatro (4) oraciones.
""".strip()

_GPT_DICTAMEN_PROMPT = """
Actúa como Oficial de Cumplimiento experto en PLAFT. Realiza un dictamen integral y holístico del expediente de este cliente.
A continuación tienes la información consolidada. Respuestas del formulario KYC: [{respuestas_kyc_json}]. Resumen de los análisis de documentos adjuntos: [{resumen_ocr_documentos}].
No analices el expediente documento por documento ni página por página; realiza un cruce global. Evalúa la coherencia entre su perfil económico, el origen de sus fondos, las validaciones de identidad y el volumen de la transacción, identificando cualquier inconsistencia, bandera roja o intento de ocultamiento.
Genera una conclusión definitiva justificando tu decisión al respecto del nivel de riesgo. Tu respuesta DEBE ser un texto corrido de exactamente uno (1) o máximo dos (2) párrafos.
""".strip()

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
    user_input_id = fields.Many2one(
        'survey.user_input',
        string="Respuesta de encuesta",
        help="Última respuesta de encuesta completada por el cliente para esta evaluación.",
        copy=False,
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
    origin_funds_score = fields.Integer(string="Puntos: Origen de Fondos (1-10)", default=0)
    economic_activity_score = fields.Integer(string="Puntos: Actividad Economica (1-10)", default=0)
    pep_score = fields.Integer(string="Puntos: PEP (0-10)", default=0)                     
    nationality_score = fields.Integer(string="Puntos: Nacionalidad (1-10)", default=0)
    transaction_bulto_score = fields.Integer(string="Puntos: Transacción Bulto (1-10)", default=0)
    
    # IA y Castigo
    ai_penalty = fields.Integer(string="Castigo IA (Macos)", default=0) 
    ai_report = fields.Html(string="Resumen del Chivatazo IA")

    # Resultado Final Computado (R-14: escala 0-5, techo máximo 5.0)
    risk_score = fields.Float(string="Score Total (0-5)", compute="_compute_total_risk", store=True)
    risk_score_max = fields.Float(string="Máximo score", default=5.0)
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
    kyc_raw_json = fields.Text(
        string="Datos KYC (JSON)",
        help="Copia en formato JSON de todas las respuestas del formulario KYC Persona Física.",
    )
    # Resumen KYC (campos clave traídos desde el formulario PF)
    kyc_income_origin_category = fields.Selection(
        [
            ('asalariado', 'Asalariado'),
            ('jubilado', 'Jubilado'),
            ('independiente', 'Independiente'),
            ('otro', 'Otro'),
        ],
        string="KYC: Origen de los ingresos",
        help="Categoría de origen de ingresos declarada en el formulario KYC.",
    )
    kyc_expected_monthly_range = fields.Selection(
        [
            ('rango_1_10000', '1-10,000'),
            ('rango_10001_100000', '10,001-100,000'),
            ('rango_100001_250000', '100,001-250,000'),
            ('rango_250001_500000', '250,001-500,000'),
            ('rango_500001_1000000', '500,001-1,000,000'),
            ('rango_1000001_mas', '1,000,001 o más'),
        ],
        string="KYC: Rango monto mensual esperado (DOP)",
        help="Rango de monto mensual esperado declarado en el formulario KYC.",
    )
    kyc_product_type = fields.Selection(
        [
            ('bajo_costo', 'Inmueble de bajo costo'),
            ('alto_costo', 'Inmueble de alto costo'),
            ('otro', 'Otro'),
        ],
        string="KYC: Tipo de adquisición",
        help="Tipo de adquisición o producto declarado en el formulario KYC.",
    )
    kyc_international_transfers_flag = fields.Boolean(
        string="KYC: Realiza transferencias internacionales",
        help="Indica si el cliente declaró realizar transferencias internacionales.",
    )
    kyc_transfers_country_id = fields.Many2one(
        'res.country',
        string="KYC: País de transferencias internacionales",
        help="País de origen de las transferencias internacionales declaradas.",
    )
    kyc_is_pep_flag = fields.Boolean(
        string="KYC: Es o ha sido PEP",
        help="Marcado si el cliente declaró ser o haber sido PEP o figura pública.",
    )
    kyc_has_pep_link_flag = fields.Boolean(
        string="KYC: Tiene vínculo con PEP",
        help="Marcado si el cliente declaró tener vínculo familiar o societario con una PEP.",
    )
    kyc_project_name = fields.Char(
        string="KYC: Nombre del proyecto",
        help="Nombre del proyecto o fideicomiso asociado a la adquisición.",
    )
    kyc_purpose_summary = fields.Text(
        string="KYC: Propósito de la adquisición",
        help="Resumen del propósito de la adquisición o inversión declarado por el cliente.",
    )
    purchase_capacity_monthly = fields.Float(
        string="Capacidad de adquisición (mensual)",
        compute="_compute_purchase_capacity_monthly",
        store=True,
        help="Salario + otros ingresos del contacto (mensual).",
    )
    ai_cost_total = fields.Float(
        string="Costo total IA (USD)",
        compute="_compute_ai_cost_total",
        store=True,
    )
    ai_cost_ocr_usd = fields.Float(
        string="Costo IA OCR (USD)",
        compute="_compute_ai_cost_total",
        store=True,
    )
    ai_cost_dictamen_usd = fields.Float(
        string="Costo IA dictamen (USD)",
        default=0.0,
    )
    ai_dictamen_tokens = fields.Integer(
        string="Tokens IA dictamen",
        default=0,
    )
    kyc_answers_preview = fields.Html(
        string="Respuestas KYC (vista rápida)",
        compute="_compute_kyc_answers_preview",
        sanitize=False,
    )

    pending_documents_count = fields.Integer(
        string="Documentos pendientes",
        compute="_compute_pending_documents_count",
    )

    # Cache simple para mapear título de pregunta -> sección (desde CSV KYC PF)
    _KYC_SECTION_CACHE = None

    @api.depends("user_input_id")
    def _compute_pending_documents_count(self):
        PendingDoc = self.env["compliance.kyc.pending.document"].sudo()
        for rec in self:
            rec.pending_documents_count = PendingDoc.search_count(
                [
                    ("assessment_id", "=", rec.id),
                    ("status", "=", "pending"),
                ]
            )

    def _build_ai_profile_prompt(self):
        self.ensure_one()
        return _PROMPT_PERFIL_OSINT.format(
            kyc_nombre=self.partner_id.name or '',
            kyc_nacionalidad=self.partner_id.country_id.name or '',
            kyc_ocupacion=getattr(self, "kyc_actividad_economica", False) or (self.profile_id.name or ''),
        )

    @api.depends('partner_id', 'partner_id.monthly_salary', 'partner_id.other_income')
    def _compute_purchase_capacity_monthly(self):
        for rec in self:
            if rec.partner_id:
                rec.purchase_capacity_monthly = (rec.partner_id.monthly_salary or 0) + (rec.partner_id.other_income or 0)
            else:
                rec.purchase_capacity_monthly = 0.0

    @api.depends(
        'user_input_id',
        'user_input_id.user_input_line_ids',
        'user_input_id.user_input_line_ids.answer_type',
        'user_input_id.user_input_line_ids.value_char_box',
        'user_input_id.user_input_line_ids.value_text_box',
        'user_input_id.user_input_line_ids.value_numerical_box',
        'user_input_id.user_input_line_ids.value_date',
        'user_input_id.user_input_line_ids.value_datetime',
        'user_input_id.user_input_line_ids.suggested_answer_id',
        'kyc_raw_json',
    )
    def _compute_kyc_answers_preview(self):
        """Construye una vista rápida HTML de preguntas y respuestas KYC, agrupadas por sección."""

        section_map = self._load_kyc_sections()
        for rec in self:
            qa_html = ""
            try:
                sections = {}
                no_answer_label = _("No respondida")

                # Preferimos la estructura real de la encuesta para mostrar TODAS las secciones/preguntas.
                if rec.survey_id:
                    SurveyQuestion = self.env["survey.question"].sudo()
                    all_questions = SurveyQuestion.search(
                        [("survey_id", "=", rec.survey_id.id)],
                        order="sequence,id",
                    )

                    line_by_qid = {}
                    if rec.user_input_id:
                        for line in rec.user_input_id.user_input_line_ids:
                            if line.question_id:
                                line_by_qid[line.question_id.id] = line

                    current_section = _("Otras preguntas")
                    for q in all_questions:
                        if getattr(q, "is_page", False):
                            current_section = (q.title or "").strip() or _("Otras preguntas")
                            sections.setdefault(current_section, [])
                            continue

                        question_title = (q.title or "").strip()
                        if not question_title:
                            continue

                        line = line_by_qid.get(q.id)
                        answer = ""
                        if line:
                            raw = getattr(line, "suggested_answer_id", None) and line.suggested_answer_id.value
                            if raw:
                                answer = rec._format_kyc_answer(raw)
                            else:
                                raw = (
                                    getattr(line, "value_char_box", None)
                                    or getattr(line, "value_text_box", None)
                                )
                                if raw is not None and str(raw).strip():
                                    answer = rec._format_kyc_answer(raw)
                                elif getattr(line, "value_numerical_box", None) is not None:
                                    answer = str(line.value_numerical_box)
                                elif getattr(line, "value_date", None):
                                    answer = str(line.value_date)
                                elif getattr(line, "value_datetime", None):
                                    answer = str(line.value_datetime)
                                else:
                                    answer = ""

                        sections.setdefault(current_section, []).append(
                            (question_title, (answer or "").strip() or no_answer_label)
                        )
                else:
                    # Fallback cuando no hay encuesta asociada.
                    data = rec._get_kyc_qa_report_data()
                    qa_list = data.get("qa_list") or []
                    for item in qa_list:
                        q = (item.get("question") or "").strip()
                        a = (item.get("answer") or "").strip()
                        if not q:
                            continue
                        key = q.lower()
                        section = section_map.get(key) or _("Otras preguntas")
                        sections.setdefault(section, []).append((q, a or no_answer_label))

                if sections:
                    blocks = []
                    for section_name, items in sections.items():
                        header = section_name or _("Otras preguntas")
                        if section_name and section_name[0].isdigit():
                            header = section_name
                        # Tabla horizontal por sección: Pregunta | Respuesta (ancho completo)
                        rows = [
                            "<tr>"
                            "<th style='text-align:left;padding:10px 14px;border-bottom:1px solid #e2e8f0;"
                            "background:#0f172a;color:#f9fafb;font-weight:600;width:35%%;'>%s</th>"
                            "<th style='text-align:left;padding:10px 14px;border-bottom:1px solid #e2e8f0;"
                            "background:#0f172a;color:#f9fafb;font-weight:600;width:65%%;'>%s</th>"
                            "</tr>"
                            % (_("Pregunta"), _("Respuesta"))
                        ]
                        for q, a in items:
                            rows.append(
                                "<tr>"
                                "<td style='vertical-align:top;padding:8px 14px;border-bottom:1px solid #e5e7eb;"
                                "background:#f9fafb;color:#111827;width:35%%;'>%s</td>"
                                "<td style='vertical-align:top;padding:8px 14px;border-bottom:1px solid #e5e7eb;"
                                "background:#ffffff;color:#111827;width:65%%;'>%s</td>"
                                "</tr>"
                                % (html.escape(q), html.escape(a) or "<span style='color:#9ca3af;'>%s</span>" % no_answer_label)
                            )
                        table_html = (
                            "<table style='width:100%%;border-collapse:collapse;font-size:13px;"
                            "margin-top:6px;table-layout:fixed;border-radius:6px;overflow:hidden;'>%s</table>"
                            % "".join(rows)
                        )
                        block = (
                            "<details style='margin-bottom:12px;border:1px solid #e2e8f0;border-radius:10px;"
                            "width:100%%;box-sizing:border-box;background:#f8fafc;box-shadow:0 1px 2px rgba(15,23,42,0.04);'>"
                            "<summary style='padding:10px 14px;cursor:pointer;font-weight:600;font-size:13px;"
                            "background:linear-gradient(90deg,#0f172a,#1e293b);color:#f9fafb;border-radius:9px 9px 0 0;"
                            "list-style:none;display:flex;align-items:center;justify-content:space-between;'>"
                            "<span>%s</span>"
                            "<span style='font-size:11px;opacity:0.85;'>%s</span>"
                            "</summary>"
                            "<div style='padding:4px 10px 10px;width:100%%;box-sizing:border-box;background:#f8fafc;'>%s</div>"
                            "</details>"
                            % (html.escape(header), _("%s preguntas") % len(items), table_html)
                        )
                        blocks.append(block)
                    qa_html = (
                        "<div class='o_kyc_answers_preview' "
                        "style='width:100%%;min-width:100%%;max-width:100%%;box-sizing:border-box;display:block;"
                        "font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",system-ui,sans-serif;"
                        "font-size:13px;color:#0f172a;background:#f3f4f6;padding:8px 10px;border-radius:10px;'>"
                        "<div style='margin-bottom:6px;font-size:13px;font-weight:600;color:#111827;'>%s</div>"
                        "%s"
                        "</div>"
                    ) % (_("Resumen de respuestas KYC"), "".join(blocks))
            except Exception:
                qa_html = ""
            rec.kyc_answers_preview = qa_html or False

    @api.model
    def _load_kyc_sections(self):
        """Carga mapa {titulo_pregunta_lower: nombre_seccion} desde el CSV KYC PF (cacheado)."""
        if ComplianceAssessment._KYC_SECTION_CACHE is not None:
            return ComplianceAssessment._KYC_SECTION_CACHE
        mapping = {}
        try:
            base_dir = os.path.dirname(__file__)
            csv_path = os.path.join(base_dir, "..", "data", "kyc_pf_persona_fisica.csv")
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    section = (row.get("Sección") or "").strip()
                    label = (row.get("Nombre del Campo") or "").strip()
                    if not label:
                        continue
                    key = label.lower()
                    mapping[key] = section or ""
        except Exception:
            mapping = {}
        ComplianceAssessment._KYC_SECTION_CACHE = mapping
        return mapping

    def _get_kyc_qa_grouped_for_report(self):
        """Devuelve preguntas y respuestas agrupadas por sección para el reporte PDF.

        Formato:
        [
          {'name': '1. DATOS GENERALES DEL CLIENTE', 'items': [{'question': '...', 'answer': '...'}, ...]},
          ...
        ]
        """
        self.ensure_one()
        data = self._get_kyc_qa_report_data()
        qa_list = data.get('qa_list') or []
        section_map = self._load_kyc_sections()

        sections = {}
        for item in qa_list:
            q = (item.get('question') or '').strip()
            a = (item.get('answer') or '').strip()
            if not q:
                continue
            key = q.lower()
            section_name = section_map.get(key) or _("Otras preguntas")
            sections.setdefault(section_name, []).append({
                "question": q,
                "answer": a,
            })

        # Orden sencillo: por nombre de sección tal como viene del CSV
        grouped = []
        for name, items in sections.items():
            grouped.append({
                "name": name or _("Otras preguntas"),
                "items": items,
            })
        return grouped

    @api.depends('document_analysis_ids.ai_cost_usd', 'ai_cost_dictamen_usd')
    def _compute_ai_cost_total(self):
        for rec in self:
            ocr_total = 0.0
            for da in rec.document_analysis_ids:
                ocr_total += getattr(da, "ai_cost_usd", 0.0) or 0.0
            rec.ai_cost_ocr_usd = ocr_total
            rec.ai_cost_total = ocr_total + (rec.ai_cost_dictamen_usd or 0.0)

    def _get_thresholds(self):
        """Devuelve umbrales y pesos del perfil asignado o por defecto (para score, semáforo y alertas)."""
        self.ensure_one()
        # #region agent log
        _debug_log("076c93", "H1", "compliance.py:_get_thresholds", "entry", {"rec_id": self.id, "profile_id": self.profile_id.id if self.profile_id else None})
        # #endregion
        if self.profile_id:
            p = self.profile_id
            # Umbrales en escala 0-5; si vienen en escala antigua 0-100, convertir
            sm, sh = p.score_threshold_medium, p.score_threshold_high
            if (sm or 0) > 10 or (sh or 0) > 10:
                sm, sh = (sm or 36) / 20.0, (sh or 71) / 20.0
            return {
                'volume_limit': p.volume_limit_usd,
                'transfer_limit': p.transfer_count_limit,
                'score_medium': sm,
                'score_high': sh,
                'w_origin': p.weight_origin_funds,
                'w_activity': p.weight_economic_activity,
                'w_pep': p.weight_pep,
                'w_geo': p.weight_nationality,
                'w_volume': p.weight_transaction_bulto,
            }
        # Umbrales en escala 0-5 (R-14)
        return {
            'volume_limit': 15000.0,
            'transfer_limit': 3,
            'score_medium': 1.8,
            'score_high': 3.5,
            'w_origin': 0.30,
            'w_activity': 0.25,
            'w_pep': 0.20,
            'w_geo': 0.15,
            'w_volume': 0.10,
        }

    # -------------------------------------------------------------------------
    # Helpers de reporte
    # -------------------------------------------------------------------------
    def _get_kyc_pf_report_data(self):
        """Prepara datos para el reporte KYC Persona Física.

        Devuelve un dict con:
        - kyc: dict de respuestas KYC (x_kyc_*) parseado desde kyc_raw_json.
        - partner: datos básicos del contacto (nombre, documento, dirección, etc.).
        - company_logo: logo de la compañía en base64 para el encabezado del reporte.
        """
        self.ensure_one()
        kyc = {}
        if self.kyc_raw_json:
            try:
                kyc = json.loads(self.kyc_raw_json) or {}
            except Exception:
                kyc = {}

        partner_vals = {}
        if self.partner_id:
            partner = self.partner_id
            partner_vals = {
                'name': partner.name or '',
                'vat': partner.vat or '',
                'street': partner.street or '',
                'street2': partner.street2 or '',
                'city': partner.city or '',
                'zip': getattr(partner, 'zip', '') or '',
                'country_name': partner.country_id.name or '',
                'phone': partner.phone or '',
                'mobile': partner.mobile or '',
                'email': partner.email or '',
            }

        company_logo = ''
        # company.logo es binary (bytes); lo convertimos a str base64 si existe
        logo_binary = self.env.company.logo
        if isinstance(logo_binary, (bytes, bytearray)):
            try:
                company_logo = logo_binary.decode('utf-8')
            except Exception:
                company_logo = ''

        return {
            'kyc': kyc,
            'partner': partner_vals,
            'company_logo': company_logo,
        }

    def _format_kyc_answer(self, val):
        """Convierte valor crudo a texto legible: booleanos como Sí/No, fechas formateadas."""
        if val is None:
            return ''
        if val is True:
            return _('Sí')
        if val is False:
            return _('No')
        s = str(val).strip()
        if s.lower() == 'true':
            return _('Sí')
        if s.lower() == 'false':
            return _('No')
        return s

    def action_open_kyc_correction_wizard(self):
        """Abre el wizard para corregir las respuestas del cliente (R-06). Actualiza el formulario original."""
        self.ensure_one()
        if not self.user_input_id:
            raise UserError(_("No hay una respuesta de encuesta asociada a esta evaluación."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Corregir respuestas KYC'),
            'res_model': 'compliance.kyc.correction.wizard',
            'view_mode': 'form',
            # Usamos target='current' para que el wizard se muestre a pantalla completa
            # y aproveche las clases CSS de ancho completo definidas en el módulo.
            'target': 'current',
            'context': {
                'default_assessment_id': self.id,
                'active_id': self.id,
                'active_model': 'compliance.assessment',
            },
        }

    def action_edit_kyc_answers(self):
        """Abre la respuesta de encuesta original (vista técnica) para revisión avanzada."""
        self.ensure_one()
        if not self.user_input_id:
            raise UserError(_("No hay una respuesta de encuesta asociada a esta evaluación."))
        action = self.env['ir.actions.act_window']._for_xml_id('survey.action_survey_user_input')
        # Forzamos la vista formulario principal de participaciones
        form_view = self.env.ref('survey.survey_user_input_view_form', raise_if_not_found=False)
        if form_view:
            action['views'] = [(form_view.id, 'form')]
        action['res_id'] = self.user_input_id.id
        raw_ctx = action.get('context') or '{}'
        ctx = {}
        if isinstance(raw_ctx, dict):
            ctx = dict(raw_ctx)
        elif isinstance(raw_ctx, str):
            try:
                ctx = safe_eval(raw_ctx)
            except Exception:
                ctx = {}
        if self.partner_id:
            ctx.setdefault('default_partner_id', self.partner_id.id)
        action['context'] = ctx
        return action

    def action_open_pending_documents_wizard(self):
        """Abre el wizard para subir documentos pendientes (R-19)."""
        self.ensure_one()
        if self.pending_documents_count <= 0:
            raise UserError(_("No hay documentos pendientes para esta evaluación."))

        form_view = self.env.ref(
            "ghr_compliance.view_compliance_pending_documents_wizard_form",
            raise_if_not_found=False,
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Documentos pendientes (KYC)"),
            "res_model": "compliance.pending.documents.wizard",
            "view_mode": "form",
            "target": "new",
            "views": ([(form_view.id, "form")] if form_view else []),
            "context": {"default_assessment_id": self.id},
        }

    def _get_kyc_qa_report_data(self):
        """Lista de preguntas y respuestas tal como el usuario las envió (para R-05).

        Prioridad: user_input_id (líneas de la encuesta). Si no hay, kyc_raw_json.
        Las respuestas se muestran en formato legible (Sí/No, no True/False).
        Devuelve: {'qa_list': [{'question': str, 'answer': str}, ...], 'partner': {...}, 'company_logo': str}
        """
        self.ensure_one()
        qa_list = []
        if self.user_input_id:
            for line in self.user_input_id.user_input_line_ids.sorted(
                key=lambda l: (l.question_id.sequence if l.question_id else 0, l.question_id.id or 0, l.id)
            ):
                if not line.question_id or getattr(line.question_id, 'is_page', False):
                    continue
                title = (line.question_id.title or '').strip() or _('Pregunta')
                raw = (
                    getattr(line, 'suggested_answer_id', None) and line.suggested_answer_id.value
                )
                if raw:
                    answer = self._format_kyc_answer(raw)
                else:
                    raw = (
                        getattr(line, 'value_char_box', None)
                        or getattr(line, 'value_text_box', None)
                    )
                    if raw is not None and str(raw).strip():
                        answer = self._format_kyc_answer(raw)
                    elif getattr(line, 'value_numerical_box', None) is not None:
                        answer = str(line.value_numerical_box)
                    elif getattr(line, 'value_date', None):
                        answer = str(line.value_date)
                    elif getattr(line, 'value_datetime', None):
                        answer = str(line.value_datetime)
                    else:
                        answer = ''
                # Para preguntas de texto/número/fecha, si quedó literalmente "No",
                # lo tratamos como no respondida a nivel de visualización.
                if (line.answer_type or '') != 'suggestion' and answer.strip().lower() == 'no':
                    answer = ''
                qa_list.append({'question': title, 'answer': answer or _('No respondidada')})
        else:
            kyc = {}
            if self.kyc_raw_json:
                try:
                    kyc = json.loads(self.kyc_raw_json) or {}
                except Exception:
                    kyc = {}
            for key, val in sorted(kyc.items()):
                if val is None and not (isinstance(val, bool)):
                    continue
                if isinstance(val, str) and not val.strip() and val.lower() not in ('false', 'true'):
                    continue
                label = key.replace('x_kyc_', '').replace('_', ' ').strip().title()
                answer = self._format_kyc_answer(val)
                if not answer and val is not False and val is not True:
                    continue
                qa_list.append({'question': label, 'answer': answer or _('No respondidada')})

        partner_vals = {}
        if self.partner_id:
            p = self.partner_id
            partner_vals = {
                'name': p.name or '',
                'vat': p.vat or '',
                'email': p.email or '',
            }
        company_logo = ''
        logo_binary = self.env.company.logo
        if isinstance(logo_binary, (bytes, bytearray)):
            try:
                company_logo = logo_binary.decode('utf-8')
            except Exception:
                pass
        return {
            'qa_list': qa_list,
            'partner': partner_vals,
            'company_logo': company_logo,
        }

    def _recompute_scores_from_user_input(self):
        """Recalcula los puntajes de riesgo a partir de la respuesta de encuesta ligada.

        Se usa cuando se corrigen manualmente respuestas KYC desde el wizard, para que
        el scoring operativo (R-07) se actualice sin necesidad de que el cliente vuelva
        a enviar el formulario.
        """
        self.ensure_one()
        if not self.user_input_id:
            return

        response = self.user_input_id

        scores = {
            'funds': [],
            'pep': [],
            'activity': [],
            'geo': [],
            'volume': [],
        }
        volume_values = []
        transfer_values = []

        for line in response.user_input_line_ids:
            title = (line.question_id.title or '').strip().lower()
            points = line.answer_score if line.answer_score is not None else None

            # Volumen Mensual (USD)
            if any(k in title for k in ['volumen mensual', 'mensual (usd)', 'volumen mensual (usd)']):
                raw_num = getattr(line, 'value_numerical_box', None)
                if raw_num is None and getattr(line, 'value_char_box', None):
                    try:
                        raw_num = float(str(line.value_char_box).replace(',', '').strip())
                    except (TypeError, ValueError):
                        raw_num = None
                if raw_num is not None:
                    volume_values.append(float(raw_num))
                continue

            # Cantidad de transferencias
            if any(k in title for k in ['cantidad de transferencias', 'número de transferencias', 'numero de transferencias', 'transferencias iniciales']):
                raw_num = getattr(line, 'value_numerical_box', None)
                if raw_num is None and getattr(line, 'value_char_box', None):
                    try:
                        raw_num = float(str(line.value_char_box).replace(',', '').strip())
                    except (TypeError, ValueError):
                        raw_num = None
                if raw_num is not None:
                    transfer_values.append(int(raw_num))
                continue

            # Geografía y nacionalidad (prioridad)
            # Incluye EE.UU./US Person para capturar preguntas con scoring asociadas a nationality_score.
            if any(
                k in title
                for k in [
                    'país',
                    'nacionalidad',
                    'jurisdicción',
                    'internacionales',
                    'ee.uu',
                    'us person',
                    'residente',
                    'ciudadano',
                ]
            ):
                if points is not None:
                    scores['geo'].append(points)

            # Origen de Fondos (solo fondos/recursos/ingresos/donaciones; no capturar "País de origen...")
            elif any(k in title for k in ['fondos', 'recursos', 'ingresos', 'donaciones']):
                if points is not None:
                    scores['funds'].append(points)

            # PEP / Beneficiario Final
            elif any(k in title for k in ['pep', 'accionista', 'ejecutivo', 'beneficiario', 'compleja']):
                if points is not None:
                    scores['pep'].append(points)

            # Actividad económica
            elif any(k in title for k in ['actividad', 'negocio', 'industria', 'estado', 'fiduciaria', 'vínculos']):
                if points is not None:
                    scores['activity'].append(points)

            # Volumen transaccional / El Bulto
            elif any(k in title for k in [
                'volumen',
                'transaccional',
                'bulto',
                '250,000',
                'monto mensual',
                'rango monto mensual',
                'transacciones',
            ]):
                if points is not None:
                    scores['volume'].append(points)

        vals = {}
        if scores['funds']:
            vals['origin_funds_score'] = max(scores['funds'])
        if scores['pep']:
            vals['pep_score'] = max(scores['pep'])
        if scores['activity']:
            vals['economic_activity_score'] = max(scores['activity'])
        if scores['geo']:
            vals['nationality_score'] = max(scores['geo'])
        if scores['volume']:
            vals['transaction_bulto_score'] = max(scores['volume'])
        if volume_values:
            vals['transaccional_volume'] = max(volume_values)
        if transfer_values:
            vals['transfer_count'] = int(max(transfer_values))

        if vals:
            self.write(vals)

    def _refresh_kyc_raw_json_from_user_input(self):
        """Actualiza kyc_raw_json a partir de las líneas de la encuesta (tras correcciones)."""
        self.ensure_one()
        if not self.user_input_id:
            return
        kyc_mapping = _load_kyc_pf_mapping()
        kyc_raw = {}
        for line in self.user_input_id.user_input_line_ids:
            title = (line.question_id.title or '').strip().lower()
            if not title:
                continue
            cfg = kyc_mapping.get(title)
            if not cfg:
                continue
            tech = cfg["tech"]
            raw_val = (
                getattr(line, 'value_char_box', None)
                or getattr(line, 'value_text_box', None)
                or getattr(line, 'value_numerical_box', None)
            )
            if raw_val is None and getattr(line, 'suggested_answer_id', None) and line.suggested_answer_id.value:
                raw_val = line.suggested_answer_id.value
            if raw_val is None and getattr(line, 'value_date', None):
                raw_val = str(line.value_date)
            if raw_val is None and getattr(line, 'value_datetime', None):
                raw_val = str(line.value_datetime)
            if raw_val is None and getattr(line, 'answer_score', None) is not None:
                raw_val = line.answer_score
            if raw_val is None:
                continue
            raw_str = str(raw_val).strip()
            kyc_raw[tech] = raw_str
        try:
            self.kyc_raw_json = json.dumps(kyc_raw, ensure_ascii=False) if kyc_raw else ''
        except Exception:
            pass

    @api.depends('origin_funds_score', 'economic_activity_score', 'pep_score', 
                 'nationality_score', 'transaction_bulto_score', 'ai_penalty', 'profile_id',
                 'kyc_is_pep_flag')
    def _compute_total_risk(self):
        # #region agent log
        _debug_log("076c93", "H1", "compliance.py:_compute_total_risk", "entry", {"ids": self.ids})
        # #endregion
        RISK_CEILING = 5.0  # R-14: techo máximo absoluto 5.0 puntos
        for rec in self:
            th = rec._get_thresholds()
            # R-15: Descalificación automática (reglas de exclusión). PEP → score 5.0 sin importar el resto.
            if rec.kyc_is_pep_flag or (rec.pep_score and rec.pep_score >= 8):
                rec.risk_score = RISK_CEILING
                rec.risk_level = 'high'
                if rec.state != 'approved':
                    rec.state = 'blocked'
                continue
            # Cálculo ponderado con pesos configurables por perfil (escala interna 0-100)
            base_score = (
                (rec.origin_funds_score * th['w_origin']) +
                (rec.economic_activity_score * th['w_activity']) +
                (rec.pep_score * th['w_pep']) +
                (rec.nationality_score * th['w_geo']) +
                (rec.transaction_bulto_score * th['w_volume'])
            ) * 10

            parameter_penalty = 0.0
            partner = rec.partner_id
            if partner:
                RiskParam = rec.env['compliance.risk.parameter'].sudo()
                if partner.country_id:
                    params_country = RiskParam.search([
                        ('parameter_type', '=', 'country'),
                        ('country_id', '=', partner.country_id.id),
                        ('active', '=', True),
                    ])
                    if params_country:
                        parameter_penalty += max(params_country.mapped('base_score')) * 10
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
                params_partner = RiskParam.search([
                    ('parameter_type', '=', 'partner'),
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                ])
                if params_partner:
                    parameter_penalty += max(params_partner.mapped('base_score')) * 10

            # Penalización operativa por desproporción entre capacidad declarada y volumen mensual:
            # +0.5 puntos (escala 0-5) si volumen > 2x capacidad, +1.0 si volumen > 3x.
            # Se acumula en escala 0-100 para mantener consistencia del cálculo.
            capacity_penalty_100 = 0.0
            capacity = rec.purchase_capacity_monthly or 0.0
            volume = rec.transaccional_volume or 0.0
            if capacity > 0 and volume > 0:
                ratio = volume / capacity
                if ratio > 3.0:
                    capacity_penalty_100 = 20.0  # +1.0 sobre escala 0-5
                elif ratio > 2.0:
                    capacity_penalty_100 = 10.0  # +0.5 sobre escala 0-5

            total_100 = base_score + rec.ai_penalty + parameter_penalty + capacity_penalty_100
            # R-14: escala 0-5 y techo 5.0 (100 → 5.0)
            rec.risk_score = min(RISK_CEILING, total_100 / 20.0)

            if rec.risk_score >= th['score_high']:
                rec.risk_level = 'high'
                if rec.state != 'approved':
                    rec.state = 'blocked'
            elif rec.risk_score >= th['score_medium']:
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

    def _get_ai_documents_summary(self):
        """Resumen compacto de los diagnósticos OCR para usar en el dictamen global."""
        self.ensure_one()
        docs = self.document_analysis_ids.filtered(lambda d: d.ocr_text)
        if not docs:
            return ""
        # Reutilizamos el helper de acortar texto del modelo de análisis documental
        Shortener = self.env["compliance.document.analysis"]
        summaries = []
        for doc in docs:
            raw = doc.ocr_text or ""
            short = Shortener._shorten_text_to_sentences(raw, max_sentences=3, max_chars=400)
            label = doc.doc_type or "documento"
            summaries.append("%s: %s" % (label, short))
        return " | ".join(summaries)

    def _build_assessment_data_for_prompt(self):
        """Devuelve un texto con los datos de la evaluación para inyectar en prompts internos (si se requiere)."""
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            return ""
        nombre = partner.name or _("Cliente sin nombre")
        pais = partner.country_id.name if partner.country_id else _("No declarado")
        riesgo_map = {'low': 'BAJO', 'medium': 'MEDIO', 'high': 'ALTO'}
        riesgo = riesgo_map.get(self.risk_level or 'low')
        lines = [
            _("RESUMEN ESTRUCTURADO DE LA EVALUACIÓN:"),
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
            _("- Score total (0-5): %s") % (round(self.risk_score, 1) if self.risk_score else 0),
        ]
        return "\n".join(lines)

    def _call_gemini_api(self, api_key, prompt_text):
        """Llama a la API de Gemini y devuelve el texto generado o None si hay error."""
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
        data = None
        for attempt in range(_MAX_RETRIES_429 + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _MAX_RETRIES_429:
                    delay = _DELAY_429_SECONDS[attempt]
                    _logger.info("ghr_compliance Gemini API 429, reintento en %ss (intento %s)", delay, attempt + 1)
                    time.sleep(delay)
                    continue
                _logger.warning("ghr_compliance Gemini API HTTP error: %s", e, exc_info=True)
                if e.code == 404:
                    return None, _("Modelo no disponible (404). Verifique la API key y el nombre del modelo en Ajustes.")
                return None, _("Error HTTP %s: %s") % (e.code, e.reason or "")
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                _logger.warning("ghr_compliance Gemini API error: %s", e, exc_info=True)
                return None, _("Error de conexión o respuesta inválida.")
        if data is None:
            return None, _("Error de conexión.")
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

    def _call_gpt_api(self, api_key, prompt_text):
        """Llama a la API de OpenAI (GPT) y devuelve (texto, total_tokens, error)."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        }
        body = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": prompt_text,
                }
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
        }
        data = None
        for attempt in range(_MAX_RETRIES_429 + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _MAX_RETRIES_429:
                    delay = _DELAY_429_SECONDS[attempt]
                    _logger.info("ghr_compliance GPT API 429, reintento en %ss (intento %s)", delay, attempt + 1)
                    time.sleep(delay)
                    continue
                _logger.warning("ghr_compliance GPT API HTTP error: %s", e, exc_info=True)
                return None, 0, _("Error HTTP GPT %s: %s") % (e.code, e.reason or "")
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                _logger.warning("ghr_compliance GPT API error: %s", e, exc_info=True)
                return None, 0, _("Error de conexión o respuesta inválida (GPT).")
        if data is None:
            return None, 0, _("Error de conexión.")
        choices = data.get("choices") or []
        if not choices:
            return None, 0, _("La API GPT no devolvió respuesta.")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            return None, 0, _("Texto vacío de GPT.")

        usage = data.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        return text, total_tokens, None

    def _dictamen_text_to_html(self, text):
        """Normaliza y convierte el texto del dictamen (con **bold**) a HTML seguro.

        Se intenta mantenerlo compacto, eliminando líneas vacías y espacios
        innecesarios, sin alterar la estructura pedida en el prompt.
        """
        if not text:
            return ""
        # Normalizar espacios en blanco
        normalized = "\n".join(
            ln.strip() for ln in str(text).splitlines() if ln.strip()
        )
        escaped = html.escape(normalized)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        lines = escaped.split("\n")
        out = []
        for line in lines:
            if line:
                out.append("<p>%s</p>" % line)
        return "".join(out) if out else "<p>%s</p>" % escaped

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

    def action_report_compliance_kyc_pf(self):
        """Imprimible 1 – solo preguntas y respuestas KYC (R-08).

        No dependemos del xmlid exacto, buscamos el reporte por report_name
        para evitar errores si el ID se ha perdido o cambiado en la BD.
        """
        self.ensure_one()
        report = self.env["ir.actions.report"].sudo().search(
            [("report_name", "=", "ghr_compliance.report_compliance_kyc_pf_document")],
            limit=1,
        )
        if not report:
            raise UserError(
                _(
                    "No se encontró el reporte de preguntas y respuestas KYC. "
                    "Actualice el módulo ghr_compliance o contacte al administrador."
                )
            )
        return report.report_action(self)

    def _get_missing_documents(self):
        """Devuelve lista de documentos (upload_file) faltantes según la encuesta.

        Se basa en las preguntas de tipo upload_file del survey asociado.
        """
        self.ensure_one()
        if not self.survey_id or not self.user_input_id:
            return []

        # Si ya existe el modelo de R-18, preferimos esa fuente de verdad
        try:
            PendingDoc = self.env["compliance.kyc.pending.document"].sudo()
            missing_docs = PendingDoc.search(
                [
                    ("assessment_id", "=", self.id),
                    ("status", "=", "missing"),
                ]
            )
            if missing_docs:
                return [
                    {"title": (d.title or "").strip(), "required": bool(d.required)}
                    for d in missing_docs
                    if (d.title or "").strip()
                ]
        except Exception:
            pass

        SurveyQuestion = self.env["survey.question"].sudo()
        questions = SurveyQuestion.search([
            ("survey_id", "=", self.survey_id.id),
            ("question_type", "=", "upload_file"),
            ("is_page", "=", False),
        ], order="sequence,id")

        # Mapeo rápido pregunta_id -> línea de respuesta
        line_by_qid = {}
        for line in self.user_input_id.user_input_line_ids:
            if line.question_id:
                line_by_qid[line.question_id.id] = line

        def _line_has_upload(line):
            if not line:
                return False
            # Compatibilidad entre versiones / campos
            for fname in (
                "value_attachment_ids",
                "attachment_ids",
                "value_file_upload_ids",
                "value_file_upload_id",
                "value_binary",
                "value_attachment_id",
            ):
                if not hasattr(line, fname):
                    continue
                val = getattr(line, fname)
                # Recordset / list
                try:
                    if val and hasattr(val, "__len__") and len(val):
                        return True
                except Exception:
                    pass
                # scalar
                if isinstance(val, (int, str)) and str(val).strip():
                    return True
            return False

        missing = []
        for q in questions:
            line = line_by_qid.get(q.id)
            has_file = _line_has_upload(line)
            if not has_file:
                missing.append({
                    "title": (q.title or "").strip(),
                    "required": bool(getattr(q, "constr_mandatory", False)),
                })
        return missing

    def _send_completed_kyc_email(self, to_email=None):
        """R-11/R-12: Envía al cliente el PDF de respuestas y lista de faltantes."""
        self.ensure_one()
        raw = to_email or (self.partner_id.email if self.partner_id else "") or ""
        to_email = (str(raw).strip() if raw not in (None, False) else "") or ""
        if not to_email or to_email in ("0", "0.0"):
            return False

        # Render PDF del imprimible de respuestas
        report = self.env["ir.actions.report"].sudo().search(
            [("report_name", "=", "ghr_compliance.report_compliance_kyc_pf_document")],
            limit=1,
        )
        pdf_attachment_id = False
        if report:
            try:
                pdf_content, _content_type = report._render_qweb_pdf([self.id])
                if pdf_content:
                    att = self.env["ir.attachment"].sudo().create({
                        "name": "KYC_PF_%s.pdf" % (self.name or "Evaluacion"),
                        "type": "binary",
                        "datas": base64.b64encode(pdf_content),
                        "mimetype": "application/pdf",
                        "res_model": "compliance.assessment",
                        "res_id": self.id,
                    })
                    pdf_attachment_id = att.id
            except Exception:
                pdf_attachment_id = False

        PendingDoc = self.env["compliance.kyc.pending.document"].sudo()
        pending_docs = PendingDoc.search(
            [
                ("assessment_id", "=", self.id),
                ("status", "=", "pending"),
            ],
        )

        missing = self._get_missing_documents()
        missing_required = [m["title"] for m in missing if m.get("required")]
        missing_optional = [m["title"] for m in missing if not m.get("required")]

        pending_html = ""
        if pending_docs:
            pending_required = []
            pending_optional = []
            for d in pending_docs:
                dl = ""
                if d.deadline:
                    try:
                        dl_dt = fields.Datetime.context_timestamp(self.env.user, d.deadline)
                        dl = dl_dt.strftime("%d/%m/%Y %H:%M")
                    except Exception:
                        dl = ""
                title = (d.title or "").strip()
                if not title:
                    continue
                if dl:
                    title = "%s (vence: %s)" % (title, dl)
                if d.required:
                    pending_required.append(title)
                else:
                    pending_optional.append(title)

            parts = []
            if pending_required:
                parts.append(
                    "<p style='margin:10px 0 6px;'><b>Documentos requeridos pendientes (\"Adjuntaré más tarde\"):</b></p>"
                    "<ul style='margin:0 0 8px 18px;'>%s</ul>"
                    % ("".join("<li>%s</li>" % html.escape(t) for t in pending_required))
                )
            if pending_optional:
                parts.append(
                    "<p style='margin:10px 0 6px;'><b>Documentos adicionales pendientes (\"Adjuntaré más tarde\"):</b></p>"
                    "<ul style='margin:0 0 8px 18px;'>%s</ul>"
                    % ("".join("<li>%s</li>" % html.escape(t) for t in pending_optional))
                )
            pending_html = "".join(parts)

        missing_html = ""
        if missing_required or missing_optional:
            parts = []
            if missing_required:
                parts.append(
                    "<p style='margin:10px 0 6px;'><b>Documentos requeridos faltantes:</b></p>"
                    "<ul style='margin:0 0 8px 18px;'>%s</ul>"
                    % ("".join("<li>%s</li>" % html.escape(t) for t in missing_required))
                )
            if missing_optional:
                parts.append(
                    "<p style='margin:10px 0 6px;'><b>Documentos adicionales sugeridos:</b></p>"
                    "<ul style='margin:0 0 8px 18px;'>%s</ul>"
                    % ("".join("<li>%s</li>" % html.escape(t) for t in missing_optional))
                )
            missing_html = "".join(parts)

        subject = _("Confirmación: formulario KYC recibido (%s)") % (self.name or "")
        company_name = self.env.company.name or _("Su entidad")
        partner_name = self.partner_id.name or ""
        body_html = (
            "<div style='font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;"
            "font-size:14px;color:#111827;background:#f3f4f6;padding:16px;'>"
            "<div style='max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;"
            "box-shadow:0 1px 3px rgba(15,23,42,0.08);overflow:hidden;'>"
            "<div style='padding:16px 20px;border-bottom:1px solid #e5e7eb;'>"
            "<h2 style='margin:0;font-size:18px;color:#111827;'>Confirmación de recepción de formulario KYC</h2>"
            "<p style='margin:6px 0 0;font-size:13px;color:#4b5563;'>%s</p>"
            "</div>"
            "<div style='padding:18px 20px;'>"
            "<p style='margin:0 0 10px;'>Estimado/a %s,</p>"
            "<p style='margin:0 0 12px;line-height:1.5;'>Hemos recibido correctamente su formulario de Debida Diligencia (KYC). "
            "En el archivo PDF adjunto encontrará un resumen imprimible de todas las respuestas suministradas.</p>"
            "%s"
            "<p style='margin:12px 0 0;line-height:1.5;'>Agradecemos su colaboración. "
            "Ante cualquier duda o corrección, puede responder directamente a este correo.</p>"
            "</div>"
            "<div style='padding:10px 20px;border-top:1px solid #e5e7eb;font-size:11px;color:#6b7280;'>"
            "<p style='margin:0;'>Este mensaje fue emitido automáticamente por el sistema de Cumplimiento de %s.</p>"
            "</div>"
            "</div>"
            "</div>"
        ) % (company_name, partner_name, (pending_html + missing_html) or "", company_name)

        vals = {
            "model": "compliance.assessment",
            "res_id": self.id,
            "email_to": to_email,
            "email_from": self.env.company.email or self.env.user.email or False,
            "subject": subject,
            "body_html": body_html,
        }
        if pdf_attachment_id:
            vals["attachment_ids"] = [(6, 0, [pdf_attachment_id])]
        self.env["mail.mail"].sudo().create(vals).send()
        # Evidencia interna en la evaluación (email siempre como texto legible)
        email_display = to_email if isinstance(to_email, str) else (self.partner_id.email or _("(sin correo)"))
        self.message_post(body=_("Imprimible KYC y lista de documentos pendientes/faltantes enviados por email a %s.") % email_display)
        return True

    def action_report_compliance_scoring(self):
        """Imprimible 2 – Análisis de riesgo con scoring (R-09)."""
        self.ensure_one()
        report = self.env["ir.actions.report"].sudo().search(
            [("report_name", "=", "ghr_compliance.report_compliance_assessment_scoring")],
            limit=1,
        )
        if not report:
            raise UserError(
                _(
                    "No se encontró el reporte de análisis de riesgo y scoring. "
                    "Actualice el módulo ghr_compliance o contacte al administrador."
                )
            )
        return report.report_action(self)

    def action_generate_ai_report(self):
        """Genera el dictamen y lo guarda en ai_report.

        Prioridad:
        - Si hay API key de OpenAI (GPT), usa GPT.
        - En su defecto, si hay API key de Gemini, usa Gemini.
        - Si ninguna está configurada o falla la IA, usa dictamen por reglas internas.
        """
        params = self.env["ir.config_parameter"].sudo()
        try:
            dictamen_price_per_1k = float(
                params.get_param("ghr_compliance.dictamen_price_per_1k_tokens_usd") or 0.0
            )
        except ValueError:
            dictamen_price_per_1k = 0.0

        for rec in self:
            if not rec.partner_id:
                continue

            cfg = rec.env["ir.config_parameter"].sudo()
            gpt_key = (cfg.get_param("ghr_compliance.openai_api_key") or "").strip()
            gemini_key = (cfg.get_param("ghr_compliance.gemini_api_key") or "").strip()

            use_gpt = bool(gpt_key)
            api_key = gpt_key or gemini_key or ""

            if api_key:
                prompt = _GPT_DICTAMEN_PROMPT.format(
                    respuestas_kyc_json=rec.kyc_raw_json or '{}',
                    resumen_ocr_documentos=rec._get_ai_documents_summary(),
                )
                full_prompt = prompt
                dictamen_tokens = 0
                if use_gpt:
                    text, dictamen_tokens, err = rec._call_gpt_api(api_key, full_prompt)
                else:
                    text, err = rec._call_gemini_api(api_key, full_prompt)

                if text and not err:
                    rec.ai_report = rec._dictamen_text_to_html(text)
                    rec.ai_dictamen_tokens = int(dictamen_tokens or 0)
                    if use_gpt and dictamen_price_per_1k and dictamen_tokens:
                        rec.ai_cost_dictamen_usd = (float(dictamen_tokens) / 1000.0) * dictamen_price_per_1k
                    else:
                        rec.ai_cost_dictamen_usd = 0.0
                    continue

                fallback_html = rec._build_fallback_ai_report()
                notice = _(
                    "<p><i>No se pudo generar el dictamen con IA (GPT/Gemini) "
                    "(error o sin respuesta). Se muestra el dictamen por reglas internas. "
                    "Verifique la API key en Ajustes o la conexión.</i></p>"
                )
                rec.ai_report = notice + fallback_html
            else:
                rec.ai_report = rec._build_fallback_ai_report()
                rec.ai_cost_dictamen_usd = 0.0
                rec.ai_dictamen_tokens = 0

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
        subject = _("Invitación a formulario de Debida Diligencia (KYC)")
        company_name = self.env.company.name or _("Su entidad")
        body = (
            "<div style='font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;"
            "font-size:14px;color:#111827;background:#f3f4f6;padding:16px;'>"
            "<div style='max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;"
            "box-shadow:0 1px 3px rgba(15,23,42,0.08);overflow:hidden;'>"
            "<div style='padding:16px 20px;border-bottom:1px solid #e5e7eb;'>"
            "<h2 style='margin:0;font-size:18px;color:#111827;'>Invitación a completar formulario de Debida Diligencia</h2>"
            "<p style='margin:6px 0 0;font-size:13px;color:#4b5563;'>%s</p>"
            "</div>"
            "<div style='padding:18px 20px;'>"
            "<p style='margin:0 0 10px;'>Estimado/a,</p>"
            "<p style='margin:0 0 12px;line-height:1.5;'>Para continuar con el proceso de vinculación, le invitamos a completar "
            "su formulario de Debida Diligencia (KYC) mediante el siguiente enlace seguro:</p>"
            "<p style='margin:0 0 8px;line-height:1.5;'><strong>Antes de comenzar:</strong> Le recomendamos tener preparados "
            "los siguientes documentos en formato digital (PDF, JPG o PNG), ya que serán solicitados al final del formulario:</p>"
            "<ul style='margin:0 0 14px;padding-left:20px;line-height:1.6;'>"
            "<li>Documento de identidad vigente (cédula o pasaporte).</li>"
            "<li>Si es asalariado: carta de trabajo. Si no lo es: documentos que justifiquen el origen de los fondos.</li>"
            "<li>Estados de cuenta bancarios recientes (últimos 3 meses).</li>"
            "</ul>"
            "<p style='margin:0 0 14px;text-align:center;'>"
            "<a href=\"%s\" style='display:inline-block;padding:10px 18px;border-radius:999px;"
            "background:#111827;color:#f9fafb;text-decoration:none;font-size:13px;'>"
            "Completar formulario KYC</a></p>"
            "<p style='margin:0 0 8px;font-size:12px;color:#6b7280;text-align:center;'>"
            "Si el botón no funciona, copie y pegue este enlace en su navegador:</p>"
            "<p style='margin:0 0 4px;font-size:11px;color:#4b5563;word-break:break-all;text-align:center;'>%s</p>"
            "</div>"
            "<div style='padding:10px 20px;border-top:1px solid #e5e7eb;font-size:11px;color:#6b7280;'>"
            "<p style='margin:0;'>Este mensaje fue enviado por %s para fines de cumplimiento PLAFT.</p>"
            "</div>"
            "</div>"
            "</div>"
        ) % (company_name, fill_url, fill_url, company_name)
        mail_vals = {
            "model": "compliance.assessment",
            "res_id": self.id,
            "email_to": self.partner_id.email,
            "email_from": self.env.company.email or self.env.user.email or False,
            "subject": subject,
            "body_html": body,
        }
        self.env["mail.mail"].sudo().create(mail_vals).send()
        email_display = (self.partner_id.email and str(self.partner_id.email).strip()) or _("(sin correo)")
        if email_display in ("0", "0.0"):
            email_display = _("(sin correo)")
        self.message_post(body=_("Invitación a encuesta enviada por email a %s.") % email_display)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Enviado"),
            "message": _("Invitación enviada a %s.") % email_display,
            "type": "success",
            "sticky": False,
        }}

    @api.model
    def create(self, vals):
        # Asignar encuesta KYC PF por defecto si no se ha definido
        if not vals.get('survey_id'):
            try:
                survey = self.env.ref('ghr_compliance.survey_kyc_pf', raise_if_not_found=False)
            except ValueError:
                survey = False
            if survey:
                vals['survey_id'] = survey.id
        # Asignar código de evaluación
        if vals.get('name', 'Nuevo') == 'Nuevo':
            vals['name'] = self.env['ir.sequence'].next_by_code('compliance.assessment') or 'Nuevo'
        return super(ComplianceAssessment, self).create(vals)

    @api.model
    def _ensure_res_partner_columns(self):
        """Migración: añade columnas de cumplimiento a res_partner si faltan (al actualizar módulo)."""
        import logging
        try:
            from ..hooks import _add_res_partner_columns
            _add_res_partner_columns(self.env.cr)
        except Exception as e:
            if "lock" in str(e).lower() or "timeout" in str(e).lower() or "LockNotAvailable" in type(e).__name__:
                logging.getLogger(__name__).warning(
                    "ghr_compliance: no se pudieron añadir columnas a res_partner (lock/timeout). "
                    "Cierre otras pestañas de Odoo y actualice el módulo de nuevo. Error: %s", e
                )
            else:
                raise

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

    @api.model
    def _configure_pep_trigger_questions_data(self):
        """Configura condicionales PEP en la encuesta KYC PF (ejecutable desde XML data).

        Se ejecuta en upgrade para asegurar que los desencadenantes queden aplicados:
        - ¿Es o ha sido PEP...? == Sí  -> preguntas de detalle PEP.
        - ¿Tiene vínculo con PEP...? == Sí -> preguntas de detalle PEP vinculada.
        """
        Survey = self.env["survey.survey"].sudo()
        Question = self.env["survey.question"].sudo()
        Answer = self.env["survey.question.answer"].sudo()

        survey = Survey.search(
            [("title", "=", "KYC Persona Física - Debida Diligencia")], limit=1
        )
        if not survey:
            return True

        def _q(title):
            return Question.search(
                [
                    ("survey_id", "=", survey.id),
                    ("is_page", "=", False),
                    ("title", "=", title),
                ],
                limit=1,
            )

        def _yes(question):
            if not question:
                return Answer.browse()
            return Answer.search(
                [
                    ("question_id", "=", question.id),
                    ("value", "in", ["Sí", "Si", "sí", "si", "Yes", "yes"]),
                ],
                limit=1,
            )

        pep_main_q = _q("¿Es o ha sido PEP o figura pública?")
        pep_link_q = _q("¿Tiene vínculo con PEP o figura pública?")
        pep_main_yes = _yes(pep_main_q)
        pep_link_yes = _yes(pep_link_q)
        if not pep_main_yes or not pep_link_yes:
            return True

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

        trigger_field = None
        if "triggering_answer_ids" in Question._fields:
            trigger_field = "triggering_answer_ids"
        elif "suggested_answer_ids" in Question._fields:
            trigger_field = "suggested_answer_ids"
        if not trigger_field:
            return True

        for title in pep_dependents:
            q = _q(title)
            if q:
                q.write({trigger_field: [(6, 0, [pep_main_yes.id])]})

        for title in pep_link_dependents:
            q = _q(title)
            if q:
                q.write({trigger_field: [(6, 0, [pep_link_yes.id])]})

        return True

    @api.model
    def _sync_nationality_answers_data(self):
        """Sincroniza Nacionalidad con todos los países, orden y score de riesgo."""
        Question = self.env["survey.question"].sudo()
        Answer = self.env["survey.question.answer"].sudo()
        Country = self.env["res.country"].sudo()

        question = self.env.ref("ghr_compliance.survey_kyc_pf_p1_q9", raise_if_not_found=False)
        if not question:
            question = Question.search(
                [
                    ("survey_id.title", "=", "KYC Persona Física - Debida Diligencia"),
                    ("is_page", "=", False),
                    ("title", "=", "Nacionalidad"),
                ],
                limit=1,
            )
        if not question:
            return True

        def _norm(txt):
            raw = (txt or "").strip().lower()
            return "".join(
                ch for ch in unicodedata.normalize("NFD", raw) if unicodedata.category(ch) != "Mn"
            )

        # Base: 3 (medio), con excepciones por código ISO.
        low_risk_codes = {"DO"}
        high_risk_codes = {"CU", "HT", "RU", "VE", "NG", "IR", "KP", "SY", "AF", "YE", "MM", "SD", "SS", "BY"}
        medium_high_risk_codes = {
            "CN", "IN", "ZA", "EG", "MA", "TR", "SA", "AE", "PH", "ID", "VN", "IL", "HN", "SV", "GT", "NI",
            "JM", "TT", "PA", "UA", "PK", "IQ", "LY", "DZ", "LB",
        }

        def _score_for_country(country):
            code = (country.code or "").upper()
            if code in low_risk_codes:
                return 1
            if code in high_risk_codes:
                return 5
            if code in medium_high_risk_codes:
                return 4
            return 3

        countries = Country.search([("name", "!=", False)])
        countries_sorted = sorted(countries, key=lambda c: _norm(c.name))

        existing_answers = Answer.search([("question_id", "=", question.id)], order="sequence asc, id asc")
        answers_by_norm = {}
        for ans in existing_answers:
            answers_by_norm.setdefault(_norm(ans.value), []).append(ans)

        seq = 1
        used_answer_ids = set()
        for country in countries_sorted:
            key = _norm(country.name)
            pool = answers_by_norm.get(key, [])
            reuse = next((a for a in pool if a.id not in used_answer_ids), None)
            vals = {
                "question_id": question.id,
                "sequence": seq,
                "value": country.name,
                "answer_score": _score_for_country(country),
            }
            if reuse:
                reuse.write(vals)
                used_answer_ids.add(reuse.id)
            else:
                created = Answer.create(vals)
                used_answer_ids.add(created.id)
            seq += 1

        # Opción extra final.
        other_key = _norm("Otro")
        other_pool = answers_by_norm.get(other_key, [])
        other_reuse = next((a for a in other_pool if a.id not in used_answer_ids), None)
        other_vals = {
            "question_id": question.id,
            "sequence": seq,
            "value": "Otro",
            "answer_score": 6,
        }
        if other_reuse:
            other_reuse.write(other_vals)
            used_answer_ids.add(other_reuse.id)
        else:
            created_other = Answer.create(other_vals)
            used_answer_ids.add(created_other.id)

        # Mantener históricos al final, sin borrarlos.
        for ans in existing_answers.filtered(lambda a: a.id not in used_answer_ids):
            seq += 1
            ans.write({"sequence": seq})

        return True

    @api.model
    def _set_kyc_mandatory_rules_data(self):
        """Marca todo requerido en KYC PF, con exclusiones puntuales solicitadas."""
        Survey = self.env["survey.survey"].sudo()
        Question = self.env["survey.question"].sudo()

        survey = Survey.search([("title", "=", "KYC Persona Física - Debida Diligencia")], limit=1)
        if not survey:
            return True

        questions = Question.search([("survey_id", "=", survey.id), ("is_page", "=", False)])
        if not questions:
            return True

        # 1) Por defecto: todo requerido
        questions.write({"constr_mandatory": True})

        # 2) Excepciones por pregunta específica
        exempt_titles = {
            "Región Empresa",
            "Teléfono Empresa",
            "Nombre del Fideicomiso",
        }
        questions.filtered(lambda q: q.title in exempt_titles).write({"constr_mandatory": False})

        # 3) Excepciones por secciones completas
        exempt_section_xmlids = [
            "ghr_compliance.survey_kyc_pf_p5",  # 2.2 Referencias Comerciales y Personales
            "ghr_compliance.survey_kyc_pf_p6",  # 2.3 Clientes Principales (Independientes)
            "ghr_compliance.survey_kyc_pf_p7",  # 2.4 Proveedores Principales (Independientes)
        ]
        pages = self.env["survey.question"].browse()
        for xmlid in exempt_section_xmlids:
            page = self.env.ref(xmlid, raise_if_not_found=False)
            if page:
                pages |= page

        if pages:
            if "page_id" in Question._fields:
                Question.search([
                    ("survey_id", "=", survey.id),
                    ("is_page", "=", False),
                    ("page_id", "in", pages.ids),
                ]).write({"constr_mandatory": False})
            else:
                section_titles = {p.title for p in pages}
                for section_title in section_titles:
                    if section_title.startswith("2.2 ") or section_title.startswith("2.3 ") or section_title.startswith("2.4 "):
                        Question.search([
                            ("survey_id", "=", survey.id),
                            ("is_page", "=", False),
                            ("title", "ilike", section_title.split(" ", 1)[-1][:20]),
                        ]).write({"constr_mandatory": False})

        return True

    @api.model
    def _cron_remind_pending_documents_expiry(self):
        """R-20: cuando venza la caducidad de documentos pendientes, marcar expired y notificar."""
        PendingDoc = self.env["compliance.kyc.pending.document"].sudo()
        now = fields.Datetime.now()

        expired_docs = PendingDoc.search(
            [
                ("status", "=", "pending"),
                ("deadline", "!=", False),
                ("deadline", "<=", now),
            ]
        )
        if not expired_docs:
            return

        docs_by_assessment = {}
        for doc in expired_docs:
            docs_by_assessment.setdefault(doc.assessment_id.id, []).append(doc)

        for assessment_id, docs in docs_by_assessment.items():
            assessment = self.env["compliance.assessment"].browse(assessment_id)
            if not assessment.exists():
                continue

            # Marcar documentos como vencidos
            PendingDoc.browse([d.id for d in docs]).write({"status": "expired"})

            # Ajustar estado de la evaluación si no está aprobada
            if assessment.state != "approved":
                assessment.state = "blocked"

            # Notificar al cliente
            email_to = assessment.partner_id.email if assessment.partner_id else False
            if email_to:
                titles = sorted({(d.title or "").strip() for d in docs if (d.title or "").strip()})
                list_html = "".join("<li>%s</li>" % html.escape(t) for t in titles)
                body = (
                    "<p>Estimado/a,</p>"
                    "<p>Le informamos que se venció el plazo para adjuntar los siguientes documentos KYC:</p>"
                    "<ul>%s</ul>"
                    "<p>Puede adjuntarlos en cuanto sea posible mediante la opción de documentos pendientes en la evaluación.</p>"
                    % list_html
                )
                self.env["mail.mail"].sudo().create(
                    {
                        "model": "compliance.assessment",
                        "res_id": assessment.id,
                        "email_to": email_to,
                        "subject": _("Recordatorio: Documentos KYC vencidos (%s)") % (assessment.name or ""),
                        "body_html": body,
                    }
                ).send()

            assessment.message_post(
                body=_("Se vencieron documentos pendientes y la evaluación fue marcada como bloqueada (%s).")
                % (", ".join(sorted({(d.title or "").strip() for d in docs if (d.title or "").strip()})[:5]))
            )

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
    # R-14: escala 0-5 (ej. 1.8 = amarillo, 3.5 = rojo)
    score_threshold_medium = fields.Float(string="Score mínimo Riesgo Medio (0-5)", default=1.8)
    score_threshold_high = fields.Float(string="Score mínimo Riesgo Alto (0-5)", default=3.5)

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

    def action_clean_obsolete_mappings(self):
        """Elimina mapeos cuyo título no coincide con ninguna pregunta activa en encuestas."""
        SurveyQuestion = self.env['survey.question'].sudo()
        all_titles = set()
        for q in SurveyQuestion.search([('is_page', '=', False)]):
            if getattr(q, 'active', True):
                t = (q.title or '').strip().lower()
                if t:
                    all_titles.add(t)
        all_mappings = self.search([])
        to_remove = self.browse()
        for rec in all_mappings:
            key = (rec.survey_question_title or '').strip().lower()
            if key and key not in all_titles:
                to_remove |= rec
        if to_remove:
            to_remove.unlink()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Mapeos obsoletos eliminados'),
                    'message': _('Se eliminaron %s registro(s) cuyo título no existe en ninguna pregunta de encuesta.') % len(to_remove),
                    'type': 'success',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sin cambios'),
                'message': _('Todos los mapeos coinciden con preguntas existentes en encuestas.'),
                'type': 'info',
                'sticky': False,
            },
        }