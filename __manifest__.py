{
    'name': 'ah',
    'version': '1.0',
    'author': 'Leandro & Rhodetech',
    'depends': ['base', 'base_setup', 'mail', 'contacts', 'survey'],
    'data': [
        'security/ir.model.access.csv',
        'data/compliance_data.xml',
        'views/compliance_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': True,
}