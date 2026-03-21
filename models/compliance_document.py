# -*- coding: utf-8 -*-
import base64
import io
import json
import logging
import os
import time
import urllib.request
import urllib.error

from odoo import models, fields, api, _

try:
    from pdf2image import convert_from_bytes
    _PDF2IMAGE_AVAILABLE = True
except ImportError:
    _PDF2IMAGE_AVAILABLE = False

_logger = logging.getLogger(__name__)

# Reintentos ante 429 (Too Many Requests): esperar y volver a intentar
_MAX_RETRIES_429 = 3
_DELAY_429_SECONDS = (2, 5, 10)

_PROMPT_DOC_IDENTIDAD = """
Actúa como analista forense de cumplimiento (PLAFT). Analiza la imagen de este documento de identidad y cruza la información visual con los datos declarados por el cliente: Nombre declarado [{kyc_nombre}], Documento declarado [{kyc_documento}].
Verifica minuciosamente si el número de identidad, pasaporte o cédula y el nombre en la imagen coinciden exactamente con los datos declarados, independientemente de si la imagen es horizontal o vertical.
Haz una conclusión explícita indicando si los datos concuerdan a la perfección o si existen discrepancias, alteraciones o no se constata la información.
Devuelve ÚNICAMENTE tu diagnóstico directo en un máximo de cuatro (4) oraciones.
""".strip()

_PROMPT_DOC_ESTADO_CUENTA = """
Actúa como auditor financiero de cumplimiento. Analiza el documento adjunto (estado de cuenta, carta bancaria o estado financiero) considerando que la fecha actual o de transacción es [{fecha_actual}].
Tu tarea principal es extraer la fecha de emisión, corte o período del documento y calcular matemáticamente si corresponde a los últimos tres (3) meses, contando el mes en curso.
Indica la fecha detectada en el documento y redacta una conclusión firme sobre si el documento es VÁLIDO por antigüedad o si está VENCIDO según la regla de los 3 meses. También revisa que haya congruencia entre el banco señalado en las respuestas y la entidad que emite el documento adjunto a analizar.
Devuelve ÚNICAMENTE tu conclusión final en un máximo de cuatro (4) oraciones.
""".strip()


class ComplianceDocumentAnalysis(models.Model):
    _name = 'compliance.document.analysis'
    _description = 'Análisis documental (OCR) para cumplimiento'

    name = fields.Char(string="Nombre", compute="_compute_name", store=True)
    attachment_id = fields.Many2one('ir.attachment', string="Adjunto", required=True, ondelete='cascade')
    assessment_id = fields.Many2one('compliance.assessment', string="Evaluación", ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string="Contacto", ondelete='set null')
    doc_type = fields.Selection([
        ('id', 'Documento de Identidad'),
        ('bank', 'Estado Bancario'),
        ('contract', 'Contrato'),
        ('other', 'Otro'),
    ], string="Tipo de documento", default='other')
    ai_calls = fields.Integer(string="Llamadas IA", default=0)
    ai_tokens = fields.Integer(string="Tokens IA usados", default=0)
    ai_cost_usd = fields.Float(string="Costo IA OCR (USD)", default=0.0)
    ocr_text = fields.Text(string="Diagnóstico IA (breve, 2-3 oraciones)")

    def _build_ai_prompt(self):
        self.ensure_one()
        if self.doc_type in ('id', 'cedula', 'passport'):
            return _PROMPT_DOC_IDENTIDAD.format(
                kyc_nombre=self.assessment_id.partner_id.name or '',
                kyc_documento=self.assessment_id.partner_id.vat or '',
            )
        if self.doc_type in ('bank', 'estado_cuenta', 'estado_financiero'):
            return _PROMPT_DOC_ESTADO_CUENTA.format(
                fecha_actual=fields.Date.context_today(self),
            )
        # Fallback: prompt genérico anterior de OCR/resumen PLAFT
        return (
            "Actúa como analista de Cumplimiento. Tienes una imagen de un documento "
            "(contrato, carta, formulario, identificación, etc.).\n\n"
            "1) Lee TODO el texto visible del documento/imagen.\n"
            "2) Identifica solo la información más relevante para una evaluación PLAFT (tipo de documento, partes involucradas, datos de identificación clave, montos/fechas relevantes y cualquier indicio de riesgo geográfico o PEP).\n"
            "3) Devuelve ÚNICAMENTE un diagnóstico breve en un máximo de 2 a 3 oraciones, en texto corrido, "
            "sin listas, sin saltos de línea adicionales y sin usar formato Markdown."
        )

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
        data = None
        for attempt in range(_MAX_RETRIES_429 + 1):
            try:
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _MAX_RETRIES_429:
                    delay = _DELAY_429_SECONDS[attempt]
                    _logger.info("ghr_compliance Gemini OCR 429, reintento en %ss (intento %s)", delay, attempt + 1)
                    time.sleep(delay)
                    continue
                _logger.warning("ghr_compliance Gemini OCR HTTP error: %s", e)
                return None, _("Error API: %s") % (e.code or e.reason)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                _logger.warning("ghr_compliance Gemini OCR error: %s", e)
                return None, str(e)
        if data is None:
            return None, _("Error de conexión.")
        candidates = data.get("candidates") or []
        if not candidates:
            return None, _("La API no devolvió respuesta.")
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            return None, _("Respuesta vacía.")
        return (parts[0].get("text") or "").strip(), None

    @api.model
    def _pdf_to_page_images(self, pdf_bytes):
        """Convierte un PDF a una lista de (base64_png, mime) por página.

        Devuelve (lista_paginas, error_msg). Si error_msg no es None, la conversión
        falló (o fue parcial) y lista_paginas puede estar vacía.
        """
        if not _PDF2IMAGE_AVAILABLE or not pdf_bytes:
            return [], _("La librería pdf2image no está disponible en el servidor.")

        error_msg = None
        images = None

        # Primer intento: usar configuración por defecto de pdf2image
        try:
            images = convert_from_bytes(pdf_bytes)
        except Exception as e:
            error_msg = str(e)
            _logger.warning(
                "ghr_compliance PDF a imágenes (intento por defecto) falló: %s",
                e,
                exc_info=True,
            )

        # Segundo intento: probar con poppler_path explícito (útil en algunos entornos)
        if images is None:
            poppler_path = os.environ.get("POPPLER_PATH") or "/usr/bin"
            try:
                images = convert_from_bytes(pdf_bytes, poppler_path=poppler_path)
                error_msg = None  # Si aquí funciona, limpiamos el mensaje de error
            except Exception as e2:
                if not error_msg:
                    error_msg = str(e2)
                _logger.warning(
                    "ghr_compliance PDF a imágenes (con poppler_path=%s) falló: %s",
                    poppler_path,
                    e2,
                    exc_info=True,
                )
                return [], error_msg or _("No se pudo convertir el PDF a imágenes.")

        if not images:
            # No se obtuvo ninguna página, aunque no haya habido excepción clara
            return [], error_msg or _("El PDF no contiene páginas convertibles.")

        result = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            result.append((b64, "image/png"))
        return result, error_msg

    @api.model
    def _call_gpt_ocr(self, api_key, image_b64, mime_type, prompt_text=None):
        """Envía imagen a OpenAI GPT y devuelve (diagnóstico breve, total_tokens, error)."""
        if isinstance(image_b64, bytes):
            image_b64 = image_b64.decode("utf-8")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        }
        prompt = prompt_text or (
            "Actúa como analista de Cumplimiento. Tienes una imagen de un documento "
            "(contrato, carta, formulario, identificación, etc.).\n\n"
            "1) Lee TODO el texto visible del documento/imagen.\n"
            "2) Identifica solo la información más relevante para una evaluación PLAFT (tipo de documento, partes involucradas, datos de identificación clave, montos/fechas relevantes y cualquier indicio de riesgo geográfico o PEP).\n"
            "3) Devuelve ÚNICAMENTE un diagnóstico breve en un máximo de 2 a 3 oraciones, en texto corrido, "
            "sin listas, sin saltos de línea adicionales y sin usar formato Markdown."
        )
        data_url = "data:%s;base64,%s" % (mime_type or "image/jpeg", image_b64)
        body = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
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
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                # Reintentos suaves ante 429 (límite de cuota) y logging detallado
                if e.code == 429 and attempt < _MAX_RETRIES_429:
                    delay = _DELAY_429_SECONDS[attempt]
                    _logger.info("ghr_compliance GPT OCR 429, reintento en %ss (intento %s)", delay, attempt + 1)
                    time.sleep(delay)
                    continue
                err_body = ""
                try:
                    # Algunos proveedores devuelven JSON con más detalle del fallo
                    err_body = e.read().decode("utf-8")
                except Exception:
                    err_body = ""
                _logger.warning("ghr_compliance GPT OCR HTTP error: %s body=%s", e, err_body)
                msg = "Error API GPT %s: %s %s" % (
                    e.code,
                    e.reason or "",
                    err_body or "",
                )
                return None, 0, msg.strip()
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                _logger.warning("ghr_compliance GPT OCR error: %s", e)
                return None, 0, str(e)
        if data is None:
            return None, 0, _("Error de conexión.")
        choices = data.get("choices") or []
        if not choices:
            return None, 0, _("La API GPT no devolvió respuesta.")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            return None, 0, _("Respuesta vacía de GPT.")

        usage = data.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        return text, total_tokens, None

    @api.model
    def _shorten_text_to_sentences(self, text, max_sentences=3, max_chars=600):
        """Normaliza un texto y lo limita a N oraciones y longitud máxima.

        Se usa para que el diagnóstico OCR sea realmente breve y legible.
        """
        if not text:
            return ""
        # Normalizar espacios y saltos de línea
        cleaned = " ".join(str(text).split())
        # Separar por oraciones básicas (., ?, !)
        import re as _re
        sentences = _re.split(r'(?<=[.!?])\s+', cleaned)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return cleaned[:max_chars]
        trimmed = " ".join(sentences[:max_sentences])
        if len(trimmed) > max_chars:
            trimmed = trimmed[:max_chars].rstrip() + "…"
        return trimmed


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
        """Extrae texto de adjuntos usando OpenAI GPT o Gemini (OCR). Prioridad: OpenAI si está configurada."""
        DocAnalysis = self.env["compliance.document.analysis"]
        params = self.env["ir.config_parameter"].sudo()
        openai_key = (params.get_param("ghr_compliance.openai_api_key") or "").strip()
        gemini_key = (params.get_param("ghr_compliance.gemini_api_key") or "").strip()
        api_key = openai_key or gemini_key
        use_gpt = bool(openai_key)
        try:
            ocr_price_per_1k = float(
                params.get_param("ghr_compliance.ocr_price_per_1k_tokens_usd") or 0.0
            )
        except ValueError:
            ocr_price_per_1k = 0.0
        if not api_key:
            return {"type": "ir.actions.client", "tag": "display_notification", "params": {
                "title": _("Configuración"),
                "message": _("Configure la API key de OpenAI (GPT) o de Gemini en Ajustes > Compliance para usar OCR."),
                "type": "warning",
                "sticky": False,
            }}
        allowed_mimes = ("image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf")
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "compliance.assessment"),
            ("res_id", "=", self.id),
            ("mimetype", "in", allowed_mimes),
        ])
        if not attachments:
            return {"type": "ir.actions.client", "tag": "display_notification", "params": {
                "title": _("Sin adjuntos"),
                "message": _("Suba imágenes (JPEG, PNG, etc.) o PDF en el chatter y vuelva a intentar."),
                "type": "warning",
                "sticky": False,
            }}
        created = 0
        for att in attachments:
            existing = DocAnalysis.search([
                ("attachment_id", "=", att.id),
                ("assessment_id", "=", self.id),
            ], limit=1)
            if existing:
                continue
            if not att.datas:
                continue
            mimetype = getattr(att, "mimetype", None) or "image/jpeg"
            text = None
            err = None
            total_tokens = 0
            calls = 0
            if mimetype == "application/pdf":
                if not _PDF2IMAGE_AVAILABLE:
                    text = None
                    err = _("Para procesar PDF instale pdf2image y poppler (p. ej. apt-get install poppler-utils).")
                else:
                    try:
                        pdf_bytes = base64.b64decode(att.datas)
                    except Exception as e:
                        text = None
                        err = str(e)
                    else:
                        page_images, conv_err = DocAnalysis._pdf_to_page_images(pdf_bytes)
                        if conv_err:
                            text = None
                            err = _("No se pudo convertir el PDF a imágenes: %s") % conv_err
                        elif not page_images:
                            text = None
                            err = _("No se pudieron obtener páginas del PDF (archivo vacío o no estándar).")
                        else:
                            parts = []
                            for idx, (img_b64, img_mime) in enumerate(page_images, start=1):
                                if use_gpt:
                                    # Para PDF usamos un prompt genérico, ya que el tipo de documento puede ser mixto.
                                    page_text, page_tokens, page_err = DocAnalysis._call_gpt_ocr(
                                        api_key, img_b64, img_mime, prompt_text=None
                                    )
                                else:
                                    page_text, page_err = DocAnalysis._call_gemini_ocr(api_key, img_b64, img_mime)
                                    page_tokens = 0
                                calls += 1
                                if page_tokens:
                                    total_tokens += int(page_tokens)
                                # Acotar diagnóstico por página a 2-3 oraciones legibles
                                if page_text:
                                    page_text = DocAnalysis._shorten_text_to_sentences(page_text)
                                if page_err:
                                    parts.append(_("--- Página %s ---") % idx + "\n[Error: %s]" % page_err)
                                elif page_text:
                                    parts.append(_("--- Página %s ---") % idx + "\n" + page_text)
                                time.sleep(1)
                            text = "\n\n".join(parts) if parts else None
                            err = None
            else:
                if use_gpt:
                    # Intento simple de inferir tipo de documento según el nombre del adjunto
                    att_name = (att.name or "").lower()
                    if any(word in att_name for word in ("id", "identidad", "cedula", "cédula", "passport", "pasaporte")):
                        guessed_type = "id"
                    elif any(word in att_name for word in ("estado", "cuenta", "bancario", "banco", "bank", "financiero")):
                        guessed_type = "bank"
                    else:
                        guessed_type = "other"
                    tmp_doc = DocAnalysis.new({
                        "attachment_id": att.id,
                        "assessment_id": self.id,
                        "partner_id": self.partner_id.id,
                        "doc_type": guessed_type,
                    })
                    prompt_text = tmp_doc._build_ai_prompt()
                    text, total_tokens, err = DocAnalysis._call_gpt_ocr(api_key, att.datas, mimetype, prompt_text=prompt_text)
                else:
                    text, err = DocAnalysis._call_gemini_ocr(api_key, att.datas, mimetype)
                    total_tokens = 0
                calls += 1
                if text:
                    text = DocAnalysis._shorten_text_to_sentences(text)

            ai_cost_usd = 0.0
            if use_gpt and ocr_price_per_1k and total_tokens:
                ai_cost_usd = (float(total_tokens) / 1000.0) * ocr_price_per_1k

            DocAnalysis.create({
                "attachment_id": att.id,
                "assessment_id": self.id,
                "partner_id": self.partner_id.id,
                "doc_type": locals().get("guessed_type", "other"),
                "ocr_text": text or ("[Error: %s]" % err if err else ""),
                "ai_calls": calls,
                "ai_tokens": int(total_tokens or 0),
                "ai_cost_usd": ai_cost_usd,
            })
            created += 1
            time.sleep(2)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("OCR completado"),
            "message": _("Se procesaron %s documento(s).") % created,
            "type": "success",
            "sticky": False,
        }}

    def _build_assessment_data_for_prompt(self):
        """Incluye solo un resumen compacto del OCR en el prompt para IA.

        Evitamos pegar todo el texto OCR para no repetir información y
        mantener el prompt corto y barato en tokens.
        """
        result = super()._build_assessment_data_for_prompt()
        if not result:
            result = ""
        doc_texts = self.env["compliance.document.analysis"].search([
            ("assessment_id", "=", self.id),
            ("ocr_text", "!=", False),
        ]).mapped("ocr_text")
        if doc_texts:
            # Tomamos solo un resumen breve por documento para no saturar el prompt
            summaries = []
            for raw in doc_texts:
                summaries.append(self.env["compliance.document.analysis"]._shorten_text_to_sentences(raw))
            result += "\n\n" + _("RESUMEN OCR DE DOCUMENTOS ADJUNTOS:\n") + "\n".join(
                "- %s" % s for s in summaries if s
            )
        return result
