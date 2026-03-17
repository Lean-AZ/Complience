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
        assessment_id = self.env.context.get('active_id') or res.get('assessment_id')
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
                'value_char_box': line.value_char_box or '',
                'value_text_box': line.value_text_box or '',
                'value_numerical_box': line.value_numerical_box if line.answer_type == 'numerical_box' else 0.0,
                'value_date': line.value_date,
                'value_datetime': line.value_datetime,
                'suggested_answer_id': line.suggested_answer_id.id if line.suggested_answer_id else False,
            }))
        res['assessment_id'] = assessment.id
        res['line_ids'] = lines_vals
        return res

    def action_apply(self):
        """Cierra el asistente; las correcciones ya se propagaron al escribir cada línea."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window_close',
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
        required=False,
        ondelete='cascade',
        readonly=True,
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
    value_char_box = fields.Char(string='Respuesta (texto)')
    value_text_box = fields.Text(string='Respuesta (texto largo)')
    value_numerical_box = fields.Float(string='Respuesta (número)')
    value_date = fields.Date(string='Respuesta (fecha)')
    value_datetime = fields.Datetime(string='Respuesta (fecha y hora)')
    suggested_answer_id = fields.Many2one(
        'survey.question.answer',
        string='Respuesta (opción)',
        domain="[('question_id', '=', question_id)]",
    )
    question_id = fields.Many2one(
        'survey.question',
        related='input_line_id.question_id',
        readonly=True,
    )

    def _propagate_to_input_line(self, vals):
        """Escribe en la línea original del survey según el tipo de respuesta."""
        for line in self:
            if not line.input_line_id:
                continue
            update = {}
            answer_type = line.answer_type
            # Usamos los valores ya escritos en el wizard (line), no en vals crudo
            if answer_type == 'char_box':
                update['value_char_box'] = line.value_char_box or ''
            elif answer_type == 'text_box':
                update['value_text_box'] = line.value_text_box or ''
            elif answer_type == 'numerical_box':
                update['value_numerical_box'] = line.value_numerical_box
            elif answer_type == 'date':
                update['value_date'] = line.value_date
            elif answer_type == 'datetime':
                update['value_datetime'] = line.value_datetime
            elif answer_type == 'suggestion':
                update['suggested_answer_id'] = line.suggested_answer_id.id if line.suggested_answer_id else False
            if update:
                line.input_line_id.write(update)

    def write(self, vals):
        res = super().write(vals)
        # Cada vez que el usuario cambia una respuesta en el wizard,
        # propagamos inmediatamente el cambio al survey original.
        self._propagate_to_input_line(vals)
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._propagate_to_input_line({})
        return records
