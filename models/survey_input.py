import csv
from datetime import timedelta
import json
import os
import unicodedata

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


_KYC_PF_MAPPING_CACHE = None


def _load_kyc_pf_mapping():
    """Carga y cachea la definición del formulario KYC PF desde el CSV del módulo.

    Clave: título de la pregunta (Nombre del Campo) en minúsculas.
    Valor: dict con Nombre Técnico, tipo y campo opcional en res.partner.
    """
    global _KYC_PF_MAPPING_CACHE
    if _KYC_PF_MAPPING_CACHE is not None:
        return _KYC_PF_MAPPING_CACHE

    mapping = {}
    try:
        base_dir = os.path.dirname(__file__)
        csv_path = os.path.join(base_dir, "..", "data", "kyc_pf_persona_fisica.csv")
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tech = (row.get("Nombre Técnico del Campo") or "").strip()
                label = (row.get("Nombre del Campo") or "").strip()
                tipo = (row.get("Tipo de Campo") or "").strip().lower()
                partner_field = (row.get("Campo en res.partner") or "").strip() or None
                if not tech or not label or not tipo:
                    # Filas de encabezado de sección u otras sin campo técnico
                    continue
                title_key = label.strip().lower()
                mapping[title_key] = {
                    "tech": tech,
                    "tipo": tipo,
                    "partner_field": partner_field,
                }
    except Exception:
        # En caso de fallo, devolvemos un dict vacío para no romper el cierre de encuesta
        mapping = {}

    _KYC_PF_MAPPING_CACHE = mapping
    return mapping


class SurveyUserInput(models.Model):
    _inherit = 'survey.user_input'

    def _mark_done(self):
        """ 
        Sincronización avanzada y robusta para la Matriz de Riesgo GHR.
        Asegura que las 11 secciones alimenten el Score de forma inteligente.
        """
        # Ejecutamos la función original de Odoo
        res = super(SurveyUserInput, self)._mark_done()
        
        for response in self:
            # Buscamos la evaluación abierta para este cliente (por partner o por email) y misma encuesta
            domain = [('state', 'in', ['draft', 'ia_process'])]
            if response.survey_id:
                domain.append(('survey_id', '=', response.survey_id.id))
            if response.partner_id:
                assessment = self.env['compliance.assessment'].search(
                    domain + [('partner_id', '=', response.partner_id.id)],
                    order='create_date desc', limit=1
                )
            else:
                assessment = self.env['compliance.assessment'].browse()
            if not assessment and response.email:
                assessment = self.env['compliance.assessment'].search(
                    domain + [('partner_id.email', '=', (response.email or '').strip())],
                    order='create_date desc', limit=1
                )
            if assessment:
                kyc_mapping = _load_kyc_pf_mapping()
                kyc_raw = {}

                vals = {}
                scores = {
                    'funds': [], 'pep': [], 'activity': [],
                    'geo': [], 'volume': []
                }
                volume_values = []
                transfer_values = []

                for line in response.user_input_line_ids:
                    title = (line.question_id.title or '').strip().lower()
                    # Si una línea no tiene answer_score, no debe impactar factores del score.
                    points = line.answer_score if line.answer_score is not None else None

                    # 0. Campos numéricos directos: Volumen Mensual (USD) y Cantidad de Transferencias
                    #    En Odoo Survey las respuestas "Valor numérico" se guardan en value_numerical_box.
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

                    # 1. Geografía y Nacionalidad (prioridad: evita que "País de origen..." se vaya a fondos)
                    # Nota: también incluimos patrones EE.UU./US Person para que preguntas con scoring
                    # (cuentas, transferencias, etc.) alimenten nationality_score.
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

                    # 2. Origen de Fondos (solo "fondos/recursos/ingresos/donaciones"; no solo "origen")
                    elif any(k in title for k in ['fondos', 'recursos', 'ingresos', 'donaciones']):
                        if points is not None:
                            scores['funds'].append(points)

                    # 3. PEP y Beneficiario Final (Directriz 5)
                    elif any(k in title for k in ['pep', 'accionista', 'ejecutivo', 'beneficiario', 'compleja']):
                        if points is not None:
                            scores['pep'].append(points)

                    # 4. Actividad Económica / Joseo (Directriz 2)
                    elif any(k in title for k in ['actividad', 'negocio', 'industria', 'estado', 'fiduciaria', 'vínculos']):
                        if points is not None:
                            scores['activity'].append(points)

                    # 5. Volumen Transaccional / El Bulto (Directriz 2)
                    # Incluimos también patrones de "monto mensual" / "rango monto mensual"
                    # para capturar preguntas como "Rango monto mensual esperado (DOP)".
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

                # Aplicamos el valor máximo (Criterio Conservador de Riesgo)
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
                # Volumen Mensual (USD) y Cantidad de Transferencias desde respuestas numéricas
                if volume_values:
                    vals['transaccional_volume'] = max(volume_values)
                if transfer_values:
                    vals['transfer_count'] = int(max(transfer_values))

                # Actualizamos la matriz y dejamos evidencia en el historial
                if vals:
                    assessment.write(vals)
                    assessment.message_post(body=_("✅ Score y volúmenes actualizados automáticamente desde la Encuesta de Debida Diligencia."))
                if response.survey_id and not assessment.survey_id:
                    assessment.survey_id = response.survey_id
                # Notificación al oficial de cumplimiento
                if assessment.compliance_officer_id:
                    assessment.message_post(
                        body=_("Encuesta completada por el cliente. Revise la evaluación."),
                        message_type="notification",
                        subtype_xmlid="mail.mt_comment",
                        partner_ids=assessment.compliance_officer_id.partner_id.ids,
                    )

                # Sincronización a res.partner: campos KYC inferidos de las respuestas
                partner_vals = {}
                for line in response.user_input_line_ids:
                    title = (line.question_id.title or '').strip().lower()
                    text_val = (getattr(line, 'value_char_box', None) or getattr(line, 'value_text_box', None) or '')
                    if isinstance(text_val, str):
                        text_val = text_val.strip()
                    else:
                        text_val = ''

                    if any(k in title for k in ['ocupación', 'cargo', 'profesión']) and text_val:
                        partner_vals['occupation'] = text_val[:500] if len(text_val) > 500 else text_val
                    if any(k in title for k in ['origen', 'fondos', 'recursos']) and text_val:
                        partner_vals['origin_funds'] = text_val[:2000] if len(text_val) > 2000 else text_val
                    if any(k in title for k in ['pep', 'persona expuesta']) and line.answer_score and line.answer_score >= 7:
                        partner_vals['is_pep'] = True

                # Mapeo duro basado en el CSV KYC PF: captura TODAS las respuestas y,
                # cuando aplica, sincroniza algunos campos estándar del contacto.
                if response.partner_id:
                    for line in response.user_input_line_ids:
                        title = (line.question_id.title or '').strip().lower()
                        if not title:
                            continue
                        cfg = kyc_mapping.get(title)
                        if not cfg:
                            continue

                        tech = cfg["tech"]
                        tipo = cfg["tipo"]
                        partner_field = cfg["partner_field"]

                        # Normalizamos el valor bruto como texto (para JSON y partner)
                        # En preguntas de elección el valor está en suggested_answer_id.value
                        raw_val = (
                            getattr(line, 'value_char_box', None)
                            or getattr(line, 'value_text_box', None)
                            or getattr(line, 'value_numerical_box', None)
                        )
                        if raw_val is None and getattr(line, 'suggested_answer_id', None) and line.suggested_answer_id.value:
                            raw_val = line.suggested_answer_id.value
                        if raw_val is None and getattr(line, 'answer_score', None) is not None:
                            raw_val = line.answer_score

                        if raw_val is None:
                            continue

                        raw_str = str(raw_val).strip()
                        if not raw_str and tipo not in ('boolean',):
                            continue

                        # Siempre guardamos la copia cruda en JSON, indexada por nombre técnico.
                        kyc_raw[tech] = raw_str

                        # Opcionalmente, sincronizamos con un campo estándar del contacto.
                        if (
                            partner_field
                            and '(' not in partner_field  # ignoramos anotaciones como "name (parte)"
                            and partner_field in response.partner_id._fields
                        ):
                            f = response.partner_id._fields[partner_field]
                            value = None
                            if f.type in ('char', 'text'):
                                value = raw_str
                            elif f.type in ('float', 'monetary'):
                                try:
                                    value = float(raw_str.replace(',', ''))
                                except (TypeError, ValueError):
                                    value = None
                            elif f.type == 'integer':
                                try:
                                    value = int(float(raw_str.replace(',', '')))
                                except (TypeError, ValueError):
                                    value = None
                            elif f.type == 'many2one' and getattr(f, 'comodel_name', None) == 'res.country':
                                # Resolver nombre o código de país a res.country (Nacionalidad, País Domicilio, etc.)
                                Country = self.env['res.country']
                                country = None
                                if raw_str and len(raw_str.strip()) <= 3:
                                    country = Country.search([('code', '=', raw_str.strip().upper())], limit=1)
                                if not country and raw_str:
                                    country = Country.search([('name', 'ilike', raw_str)], limit=1)
                                value = country.id if country else None

                            if value not in (None, '') and partner_field not in partner_vals:
                                partner_vals[partner_field] = value

                if partner_vals and response.partner_id:
                    response.partner_id.write(partner_vals)

                # Copiamos un resumen KYC a campos clave de la evaluación
                # usando los nombres técnicos x_kyc_* del CSV.
                if kyc_raw:
                    # Origen de los ingresos (Selection)
                    income_origin = (kyc_raw.get('x_kyc_origen_ingresos') or '').strip().lower()
                    income_map = {
                        'asalariado': 'asalariado',
                        'jubilado': 'jubilado',
                        'independiente': 'independiente',
                        'otro': 'otro',
                    }
                    assessment.kyc_income_origin_category = income_map.get(income_origin) or False

                    # Rango monto mensual esperado (Selection)
                    monthly_range = (kyc_raw.get('x_kyc_rango_monto_mensual') or '').strip()
                    range_map = {
                        '1-10,000': 'rango_1_10000',
                        '10,001-100,000': 'rango_10001_100000',
                        '100,001-250,000': 'rango_100001_250000',
                        '250,001-500,000': 'rango_250001_500000',
                        '500,001-1,000,000': 'rango_500001_1000000',
                        '1,000,001 o más': 'rango_1000001_mas',
                    }
                    assessment.kyc_expected_monthly_range = range_map.get(monthly_range) or False

                    # Capacidad mensual (R-16/operativo):
                    # `purchase_capacity_monthly` en la evaluación se calcula con:
                    #   res.partner.monthly_salary + res.partner.other_income
                    # y hoy casi nunca se alimenta desde la encuesta.
                    monthly_capacity_map = {
                        'rango_1_10000': 5000.0,
                        'rango_10001_100000': 55000.0,
                        'rango_100001_250000': 175000.0,
                        'rango_250001_500000': 375000.0,
                        'rango_500001_1000000': 750000.0,
                        'rango_1000001_mas': 1250000.0,
                    }
                    if response.partner_id and assessment.kyc_expected_monthly_range:
                        derived_monthly = monthly_capacity_map.get(assessment.kyc_expected_monthly_range)
                        if derived_monthly is not None:
                            # No sobreescribimos otros ingresos si el partner ya los tiene.
                            partner_vals_capacity = {}
                            if not response.partner_id.monthly_salary:
                                partner_vals_capacity['monthly_salary'] = derived_monthly
                            if not response.partner_id.other_income:
                                partner_vals_capacity['other_income'] = 0.0
                            if partner_vals_capacity:
                                response.partner_id.write(partner_vals_capacity)

                    # Tipo de adquisición (Selection)
                    product_type = (kyc_raw.get('x_kyc_tipo_adquisicion') or '').strip().lower()
                    product_map = {
                        'inmueble de bajo costo': 'bajo_costo',
                        'inmueble de alto costo': 'alto_costo',
                        'otro': 'otro',
                    }
                    assessment.kyc_product_type = product_map.get(product_type) or False

                    # Transferencias internacionales (Boolean) y país
                    transfers_int = (kyc_raw.get('x_kyc_transferencias_int') or '').strip().lower()
                    if transfers_int in ('true', '1', 'sí', 'si', 'yes'):
                        assessment.kyc_international_transfers_flag = True
                    elif transfers_int in ('false', '0', 'no'):
                        assessment.kyc_international_transfers_flag = False

                    country_name = (kyc_raw.get('x_kyc_pais_transferencia') or '').strip()
                    if country_name:
                        Country = self.env['res.country']
                        country = Country.search([('name', '=', country_name)], limit=1)
                        assessment.kyc_transfers_country_id = country
                    else:
                        assessment.kyc_transfers_country_id = False

                    # PEP flags
                    is_pep = (kyc_raw.get('x_kyc_es_pep') or '').strip().lower()
                    if is_pep in ('true', '1', 'sí', 'si', 'yes'):
                        assessment.kyc_is_pep_flag = True
                    elif is_pep in ('false', '0', 'no'):
                        assessment.kyc_is_pep_flag = False

                    has_pep_link = (kyc_raw.get('x_kyc_vinculo_pep') or '').strip().lower()
                    if has_pep_link in ('true', '1', 'sí', 'si', 'yes'):
                        assessment.kyc_has_pep_link_flag = True
                    elif has_pep_link in ('false', '0', 'no'):
                        assessment.kyc_has_pep_link_flag = False

                    # Proyecto y propósito (texto informativo)
                    assessment.kyc_project_name = (kyc_raw.get('x_kyc_proyecto_nombre') or '').strip() or False
                    assessment.kyc_purpose_summary = (kyc_raw.get('x_kyc_proposito') or '').strip() or False

                    # Guardamos siempre el JSON crudo para auditoría.
                    try:
                        assessment.kyc_raw_json = json.dumps(kyc_raw, ensure_ascii=False)
                    except Exception:
                        # No romper el cierre si por alguna razón el dump falla.
                        assessment.kyc_raw_json = False

                # Copiar adjuntos subidos en la encuesta a la evaluación.
                try:
                    Attachment = self.env["ir.attachment"].sudo()
                    created_names = set()
                    # 1) value_file_data_ids (survey_upload_file)
                    for line in response.user_input_line_ids:
                        q = line.question_id
                        if not q or getattr(q, "question_type", "") != "upload_file":
                            continue
                        file_ids = getattr(line, "value_file_data_ids", None)
                        if file_ids:
                            for src in file_ids:
                                if not getattr(src, "datas", None):
                                    continue
                                name = src.name or (q.title or "Documento KYC").strip()
                                if name in created_names:
                                    name = "%s (%s)" % (name, src.id)
                                existing = Attachment.search([
                                    ("res_model", "=", "compliance.assessment"),
                                    ("res_id", "=", assessment.id),
                                    ("name", "=", name),
                                ], limit=1)
                                if existing:
                                    continue
                                Attachment.create({
                                    "name": name,
                                    "type": "binary",
                                    "datas": src.datas,
                                    "mimetype": getattr(src, "mimetype", None) or "application/octet-stream",
                                    "res_model": "compliance.assessment",
                                    "res_id": assessment.id,
                                })
                                created_names.add(name)
                    # 1b) Adjuntos vinculados a la línea (res_model survey.user_input.line)
                    for line in response.user_input_line_ids:
                        q = line.question_id
                        if not q or getattr(q, "question_type", "") != "upload_file":
                            continue
                        line_attachments = Attachment.search([
                            ("res_model", "=", "survey.user_input.line"),
                            ("res_id", "=", line.id),
                        ])
                        for src in line_attachments:
                            if not getattr(src, "datas", None):
                                continue
                            name = src.name or (q.title or "Documento KYC").strip()
                            if name in created_names:
                                name = "%s (%s)" % (name, src.id)
                            existing = Attachment.search([
                                ("res_model", "=", "compliance.assessment"),
                                ("res_id", "=", assessment.id),
                                ("name", "=", name),
                            ], limit=1)
                            if existing:
                                continue
                            Attachment.create({
                                "name": name,
                                "type": "binary",
                                "datas": src.datas,
                                "mimetype": getattr(src, "mimetype", None) or "application/octet-stream",
                                "res_model": "compliance.assessment",
                                "res_id": assessment.id,
                            })
                            created_names.add(name)

                    # 2) Fallback: adjuntos con res_model survey.user_input
                    src_attachments = Attachment.search([
                        ("res_model", "=", "survey.user_input"),
                        ("res_id", "=", response.id),
                    ])
                    for src in src_attachments:
                        if not src.datas:
                            continue
                        name = src.name or "Documento KYC"
                        if name in created_names:
                            name = "%s (%s)" % (name, src.id)
                        existing = Attachment.search([
                            ("res_model", "=", "compliance.assessment"),
                            ("res_id", "=", assessment.id),
                            ("name", "=", name),
                        ], limit=1)
                        if existing:
                            continue
                        Attachment.create({
                            "name": name,
                            "type": "binary",
                            "datas": src.datas,
                            "mimetype": src.mimetype or "application/octet-stream",
                            "res_model": "compliance.assessment",
                            "res_id": assessment.id,
                        })
                        created_names.add(name)

                    # 3) Fallback: value_binary en la línea (otras implementaciones)
                    for line in response.user_input_line_ids:
                        q = line.question_id
                        if not q or getattr(q, "question_type", "") != "upload_file":
                            continue
                        if getattr(line, "value_file_data_ids", None):
                            continue
                        bin_val = getattr(line, "value_binary", None)
                        if not bin_val:
                            continue
                        name = (q.title or "Documento KYC").strip()
                        if name in created_names:
                            continue
                        existing = Attachment.search([
                            ("res_model", "=", "compliance.assessment"),
                            ("res_id", "=", assessment.id),
                            ("name", "=", name),
                        ], limit=1)
                        if existing:
                            continue
                        Attachment.create({
                            "name": name,
                            "type": "binary",
                            "datas": bin_val,
                            "mimetype": "application/octet-stream",
                            "res_model": "compliance.assessment",
                            "res_id": assessment.id,
                        })
                        created_names.add(name)

                    if created_names:
                        assessment.message_post(
                            body=_("Documentos adjuntos de la encuesta copiados a esta evaluación (%s archivo(s)): %s.")
                            % (len(created_names), ", ".join(sorted(created_names)[:5]) + ("…" if len(created_names) > 5 else "")),
                        )
                except Exception:
                    pass

                # R-18: registrar documentos pendientes / faltantes (R-17: "Adjuntaré más tarde")
                try:
                    PendingDoc = self.env["compliance.kyc.pending.document"].sudo()

                    expiry_hours_raw = (
                        self.env["ir.config_parameter"].sudo().get_param(
                            "ghr_compliance.pending_docs_expiry_hours", "48"
                        )
                        or "48"
                    )
                    try:
                        expiry_hours = float(expiry_hours_raw)
                    except ValueError:
                        expiry_hours = 48.0

                    def _norm(txt):
                        s = (txt or "").strip()
                        if not s:
                            return ""
                        s = unicodedata.normalize("NFKD", str(s)).casefold()
                        return "".join(ch for ch in s if not unicodedata.combining(ch))

                    pending_created = 0
                    # Orden estable para correlacionar "upload_file" con la opción "Adjuntaré más tarde" cercana
                    ordered_lines = sorted(
                        response.user_input_line_ids,
                        key=lambda l: (
                            l.question_id.sequence if l.question_id else 0,
                            l.question_id.id if l.question_id else 0,
                            l.id,
                        ),
                    )

                    for idx, line in enumerate(ordered_lines):
                        q = line.question_id
                        if not q or getattr(q, "is_page", False):
                            continue
                        if getattr(q, "question_type", "") != "upload_file":
                            continue

                        # Determinamos si realmente se subió algo en esta línea
                        has_file = False
                        file_data_ids = getattr(line, "value_file_data_ids", None)
                        if file_data_ids:
                            for src in file_data_ids:
                                if getattr(src, "datas", None):
                                    has_file = True
                                    break
                        if not has_file:
                            bin_val = getattr(line, "value_binary", None)
                            if bin_val:
                                has_file = True

                        if has_file:
                            continue

                        # Buscamos en las líneas cercanas la opción "Adjuntaré más tarde" para este documento
                        later_selected = False
                        for j in range(idx + 1, len(ordered_lines)):
                            nl = ordered_lines[j]
                            nq = nl.question_id
                            if not nq or getattr(nq, "is_page", False):
                                continue
                            if getattr(nq, "question_type", "") == "upload_file":
                                break
                            if "adjuntare mas tarde" in _norm(getattr(nq, "title", "")):
                                chosen = (
                                    getattr(getattr(nl, "suggested_answer_id", None), "value", None)
                                    or getattr(nl, "value_char_box", None)
                                    or getattr(nl, "value_text_box", None)
                                    or ""
                                )
                                later_selected = _norm(chosen) in ("si", "true", "1", "yes")
                                break

                        status = "pending" if later_selected else "missing"

                        # Preservamos "requerido" por criterio del módulo:
                        # El 1er upload (documento ID) fue el único mandatory original (sequence=201).
                        required = bool(getattr(q, "sequence", 0) == 201)

                        deadline = (
                            fields.Datetime.now() + timedelta(hours=expiry_hours)
                            if expiry_hours and expiry_hours > 0
                            else False
                        )

                        existing = PendingDoc.search(
                            [
                                ("assessment_id", "=", assessment.id),
                                ("survey_question_id", "=", q.id),
                            ],
                            limit=1,
                        )
                        vals_pending = {
                            "required": required,
                            "status": status,
                            "deadline": deadline,
                        }
                        if existing:
                            existing.write(vals_pending)
                        else:
                            PendingDoc.create(
                                dict(
                                    assessment_id=assessment.id,
                                    survey_question_id=q.id,
                                    **vals_pending,
                                )
                            )
                            pending_created += 1

                    if pending_created:
                        assessment.message_post(
                            body=_("Se registraron documentos pendientes/faltantes (%s) desde la encuesta.")
                            % pending_created
                        )
                except Exception:
                    pass

                # Vincular la respuesta de encuesta a la evaluación para reportes (preguntas/respuestas).
                assessment.user_input_id = response.id
                # R-11/R-12: enviar automáticamente al cliente el imprimible y faltantes
                try:
                    assessment._send_completed_kyc_email(to_email=(response.partner_id.email or '').strip())
                except Exception:
                    # No romper el cierre de encuesta por un fallo de correo
                    pass

        return res


class SurveyUserInputLine(models.Model):
    _inherit = "survey.user_input.line"

    # Forzamos dominio por modelo para que el selector (incluyendo "Buscar más...")
    # solo muestre respuestas de la pregunta actual.
    suggested_answer_id = fields.Many2one(
        "survey.question.answer",
        domain="[('question_id', '=', question_id)]",
    )

    @api.onchange("question_id")
    def _onchange_question_id_domain_suggested_answer(self):
        """Restringe el dropdown a respuestas de la misma pregunta."""
        domain = []
        if self.question_id:
            domain = [("question_id", "=", self.question_id.id)]
            if self.suggested_answer_id and self.suggested_answer_id.question_id != self.question_id:
                self.suggested_answer_id = False
        return {"domain": {"suggested_answer_id": domain}}

    @api.constrains("question_id", "suggested_answer_id")
    def _check_suggested_answer_question_match(self):
        for rec in self:
            if (
                rec.suggested_answer_id
                and rec.question_id
                and rec.suggested_answer_id.question_id
                and rec.suggested_answer_id.question_id != rec.question_id
            ):
                raise ValidationError(
                    _(
                        "La respuesta seleccionada no pertenece a la pregunta actual. "
                        "Elija una opción de esa misma pregunta."
                    )
                )

    def _prepare_vals_unomit_when_answered(self, vals):
        """Si se coloca una respuesta, quitar estado omitida y normalizar tipo."""
        new_vals = dict(vals or {})
        has_answer = False
        answer_type = new_vals.get("answer_type")

        if new_vals.get("suggested_answer_id"):
            has_answer = True
            answer_type = "suggestion"
        elif "value_text_box" in new_vals and (new_vals.get("value_text_box") or "").strip():
            has_answer = True
            answer_type = "text_box"
        elif "value_char_box" in new_vals and (new_vals.get("value_char_box") or "").strip():
            has_answer = True
            answer_type = "char_box"
        elif "value_numerical_box" in new_vals and new_vals.get("value_numerical_box") is not None:
            has_answer = True
            answer_type = "numerical_box"
        elif "value_date" in new_vals and new_vals.get("value_date"):
            has_answer = True
            answer_type = "date"
        elif "value_datetime" in new_vals and new_vals.get("value_datetime"):
            has_answer = True
            answer_type = "datetime"

        if has_answer:
            if "skipped" in self._fields and "skipped" not in new_vals:
                new_vals["skipped"] = False
            if answer_type and "answer_type" in self._fields:
                new_vals["answer_type"] = answer_type
        return new_vals

    def _sync_related_assessments(self):
        """Refresca evaluación KYC tras editar líneas de encuesta."""
        inputs = self.mapped("user_input_id").filtered(lambda r: r and r.id)
        if not inputs:
            return
        assessments = self.env["compliance.assessment"].sudo().search(
            [("user_input_id", "in", inputs.ids)]
        )
        for assessment in assessments:
            try:
                assessment._recompute_scores_from_user_input()
                assessment._refresh_kyc_raw_json_from_user_input()
            except Exception:
                continue

    @api.model_create_multi
    def create(self, vals_list):
        rows = [self._prepare_vals_unomit_when_answered(vals) for vals in vals_list]
        records = super().create(rows)
        records._sync_related_assessments()
        return records

    def write(self, vals):
        result = super().write(self._prepare_vals_unomit_when_answered(vals))
        self._sync_related_assessments()
        return result


class SurveyQuestionAnswer(models.Model):
    _inherit = "survey.question.answer"

    @api.model
    def _domain_from_context_question(self):
        """Obtiene dominio por pregunta desde contexto del selector M2O."""
        ctx = self.env.context or {}
        question_id = ctx.get("question_id") or ctx.get("default_question_id")

        # Si se abre el popup desde una línea de respuesta, usamos su pregunta.
        if not question_id and ctx.get("active_model") == "survey.user_input.line" and ctx.get("active_id"):
            line = self.env["survey.user_input.line"].sudo().browse(ctx["active_id"])
            if line.exists() and line.question_id:
                question_id = line.question_id.id

        return [("question_id", "=", int(question_id))] if question_id else []

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        args = list(args or [])
        question_domain = self._domain_from_context_question()
        if question_domain:
            args += question_domain
        return super().name_search(name=name, args=args, operator=operator, limit=limit)

    @api.model
    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None):
        domain = list(domain or [])
        question_domain = self._domain_from_context_question()
        if question_domain:
            domain += question_domain
        return super().search_read(
            domain=domain, fields=fields, offset=offset, limit=limit, order=order
        )

