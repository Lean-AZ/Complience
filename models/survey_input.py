import csv
import json
import os

from odoo import models, fields, api, _


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
            # Buscamos la evaluación abierta para este cliente exacto
            assessment = self.env['compliance.assessment'].search([
                ('partner_id', '=', response.partner_id.id),
                ('state', 'in', ['draft', 'ia_process'])
            ], limit=1)
            
            if assessment:
                kyc_mapping = _load_kyc_pf_mapping()
                kyc_raw = {}

                vals = {}
                # Agrupadores de puntuación por categoría estratégica
                scores = {
                    'funds': [], 'pep': [], 'activity': [], 
                    'geo': [], 'volume': []
                }
                # Valores numéricos para Volumen Mensual (USD) y Cantidad de Transferencias
                volume_values = []
                transfer_values = []

                for line in response.user_input_line_ids:
                    # Normalización total del título para evitar fallos de "match"
                    title = (line.question_id.title or '').strip().lower()
                    points = line.answer_score

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

                    # 1. Origen de Fondos (Directriz 2 y 5)
                    if any(k in title for k in ['origen', 'fondos', 'recursos', 'ingresos', 'donaciones']):
                        scores['funds'].append(points)
                    
                    # 2. PEP y Beneficiario Final (Directriz 5)
                    elif any(k in title for k in ['pep', 'accionista', 'ejecutivo', 'beneficiario', 'compleja']):
                        scores['pep'].append(points)
                    
                    # 3. Actividad Económica / Joseo (Directriz 2)
                    elif any(k in title for k in ['actividad', 'negocio', 'industria', 'estado', 'fiduciaria', 'vínculos']):
                        scores['activity'].append(points)
                    
                    # 4. Geografía y Nacionalidad
                    elif any(k in title for k in ['país', 'nacionalidad', 'jurisdicción', 'internacionales']):
                        scores['geo'].append(points)
                    
                    # 5. Volumen Transaccional / El Bulto (Directriz 2)
                    # Incluimos también patrones de "monto mensual" / "rango monto mensual"
                    # para capturar preguntas como "Rango monto mensual esperado (DOP)".
                    elif any(k in title for k in [
                        'volumen',
                        'transaccional',
                        'bulto',
                        '250,000',
                        'cuentas',
                        'monto mensual',
                        'rango monto mensual',
                    ]):
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

                # Vincular la respuesta de encuesta a la evaluación para reportes (preguntas/respuestas).
                assessment.user_input_id = response.id

        return res

