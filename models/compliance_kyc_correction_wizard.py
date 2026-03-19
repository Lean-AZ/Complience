# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ComplianceKycCorrectionWizard(models.TransientModel):
    _name = 'compliance.kyc.correction.wizard'
    _description = 'Corregir respuestas KYC (actualiza el formulario original)'

    assessment_id = fields.Many2one(
        'compliance.assessment',
        string='Evaluación',
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    line_ids = fields.One2many(
        'compliance.kyc.correction.wizard.line',
        'wizard_id',
        string='Respuestas a corregir',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        assessment_id = (
            self.env.context.get('active_id')
            or self.env.context.get('default_assessment_id')
            or res.get('assessment_id')
        )
        if not assessment_id:
            return res
        assessment = self.env['compliance.assessment'].browse(assessment_id)
        if not assessment.exists() or not assessment.user_input_id:
            return res
        lines_vals = []
        for line in assessment.user_input_id.user_input_line_ids.sorted(
            key=lambda l: (l.question_id.sequence if l.question_id else 0, l.question_id.id or 0, l.id)
        ):
            if not line.question_id or getattr(line.question_id, 'is_page', False):
                continue
            lines_vals.append((0, 0, {
                'input_line_id': line.id,
                'question_title': (line.question_id.title or '').strip() or _('Pregunta'),
                'answer_type': line.answer_type or '',
            }))
        res['assessment_id'] = assessment.id
        res['line_ids'] = lines_vals
        return res

    def action_apply(self):
        """Aplica todas las correcciones al formulario original y cierra el asistente.

        Las líneas del wizard editan directamente las respuestas originales del survey
        mediante campos related, por lo que aquí solo recalcualmos scoring y KYC.
        """
        self.ensure_one()
        assessment = self.assessment_id
        # Recalcular scoring y actualizar copia KYC de la evaluación
        if assessment:
            assessment._recompute_scores_from_user_input()
            assessment._refresh_kyc_raw_json_from_user_input()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Correcciones aplicadas'),
                'message': _('Los cambios se han guardado en el formulario original. El scoring y el resumen KYC se han actualizado.'),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class ComplianceKycCorrectionWizardLine(models.TransientModel):
    _name = 'compliance.kyc.correction.wizard.line'
    _description = 'Línea de corrección KYC'

    wizard_id = fields.Many2one(
        'compliance.kyc.correction.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade',
    )
    input_line_id = fields.Many2one(
        'survey.user_input.line',
        string='Respuesta original',
        required=True,
        ondelete='cascade',
    )
    question_title = fields.Char(string='Pregunta', readonly=True)
    answer_type = fields.Selection([
        ('text_box', 'Texto largo'),
        ('char_box', 'Texto'),
        ('numerical_box', 'Número'),
        ('date', 'Fecha'),
        ('datetime', 'Fecha y hora'),
        ('suggestion', 'Opción elegida'),
    ], string='Tipo', readonly=True)
    # Campos de respuesta: apuntan directamente a la línea original del survey
    value_char_box = fields.Char(
        string='Respuesta (texto)',
        related='input_line_id.value_char_box',
        readonly=False,
    )
    value_text_box = fields.Text(
        string='Respuesta (texto largo)',
        related='input_line_id.value_text_box',
        readonly=False,
    )
    value_numerical_box = fields.Float(
        string='Respuesta (número)',
        related='input_line_id.value_numerical_box',
        readonly=False,
    )
    value_date = fields.Date(
        string='Respuesta (fecha)',
        related='input_line_id.value_date',
        readonly=False,
    )
    value_datetime = fields.Datetime(
        string='Respuesta (fecha y hora)',
        related='input_line_id.value_datetime',
        readonly=False,
    )
    suggested_answer_id = fields.Many2one(
        'survey.question.answer',
        string='Respuesta (opción)',
        related='input_line_id.suggested_answer_id',
        readonly=False,
    )
    question_id = fields.Many2one(
        'survey.question',
        related='input_line_id.question_id',
        readonly=True,
    )
    value_display = fields.Char(
        string='Respuesta',
        compute='_compute_value_display',
        help='Valor actual de la respuesta para mostrar en la lista.',
    )

    @api.depends(
        'value_char_box', 'value_text_box', 'value_numerical_box',
        'value_date', 'value_datetime', 'suggested_answer_id', 'answer_type',
    )
    def _compute_value_display(self):
        for line in self:
            at = line.answer_type or ''
            if at == 'char_box' and line.value_char_box:
                line.value_display = (line.value_char_box or '').strip()[:200]
            elif at == 'text_box' and line.value_text_box:
                line.value_display = (line.value_text_box or '').strip()[:200]
            elif at == 'numerical_box' and line.value_numerical_box is not None:
                line.value_display = str(line.value_numerical_box)
            elif at == 'date' and line.value_date:
                line.value_display = str(line.value_date)
            elif at == 'datetime' and line.value_datetime:
                line.value_display = str(line.value_datetime)
            elif at == 'suggestion' and line.suggested_answer_id:
                line.value_display = (line.suggested_answer_id.value or '').strip()[:200]
            else:
                line.value_display = ''

