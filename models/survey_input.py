from odoo import models, fields, api, _

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
                        raw = getattr(line, 'value_numerical_box', None)
                        if raw is None and getattr(line, 'value_char_box', None):
                            try:
                                raw = float(str(line.value_char_box).replace(',', '').strip())
                            except (TypeError, ValueError):
                                raw = None
                        if raw is not None:
                            volume_values.append(float(raw))
                        continue

                    if any(k in title for k in ['cantidad de transferencias', 'número de transferencias', 'numero de transferencias', 'transferencias iniciales']):
                        raw = getattr(line, 'value_numerical_box', None)
                        if raw is None and getattr(line, 'value_char_box', None):
                            try:
                                raw = float(str(line.value_char_box).replace(',', '').strip())
                            except (TypeError, ValueError):
                                raw = None
                        if raw is not None:
                            transfer_values.append(int(raw))
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
                    elif any(k in title for k in ['volumen', 'transaccional', 'bulto', '250,000', 'cuentas']):
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

                if partner_vals and response.partner_id:
                    response.partner_id.write(partner_vals)

                # Mapeo configurable: compliance.question.mapping por título y form_type
                form_type = assessment.form_type or 'automotriz_pf'
                Mapping = response.env['compliance.question.mapping'].sudo()
                for line in response.user_input_line_ids:
                    title = (line.question_id.title or '').strip().lower()
                    if not title:
                        continue
                    mappings = Mapping.search([
                        '|', ('form_type', '=', form_type), ('form_type', '=', 'ambos'),
                    ])
                    for m in mappings:
                        key = (m.survey_question_title or '').strip().lower()
                        if key and key not in title:
                            continue
                        dest_field = (m.destination_field or '').strip()
                        if not dest_field:
                            continue
                        if m.destination_model == 'assessment':
                            target = assessment
                        else:
                            target = response.partner_id
                        if not target:
                            continue
                        if dest_field not in target._fields:
                            continue
                        f = target._fields[dest_field]
                        raw = None
                        if f.type in ('integer', 'float'):
                            raw = getattr(line, 'answer_score', None)
                            if raw is None and getattr(line, 'value_numerical_box', None) is not None:
                                try:
                                    raw = float(line.value_numerical_box) if f.type == 'float' else int(line.value_numerical_box)
                                except (TypeError, ValueError):
                                    pass
                        else:
                            raw = getattr(line, 'value_char_box', None) or getattr(line, 'value_text_box', None)
                            raw = (raw or '').strip() if isinstance(raw, str) else (str(raw) if raw else '')
                        if raw is not None and raw != '':
                            try:
                                target.write({dest_field: raw})
                            except (TypeError, ValueError):
                                pass
        return res