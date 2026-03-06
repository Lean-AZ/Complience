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
        
        return res