# -*- coding: utf-8 -*-

import base64

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.base.models.res_bank import sanitize_account_number

import logging
_logger = logging.getLogger(__name__)


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    # Ensure transactions can be imported only once (if the import format provides unique transaction ids)
    unique_import_id = fields.Char(string='Import ID', readonly=True, copy=False)

    _sql_constraints = [
        ('unique_import_id', 'unique (unique_import_id)', 'A bank account transactions can be imported only once !')
    ]


class AccountBankStatementImport(models.TransientModel):
    _name = 'account.bank.statement.import'
    _description = 'Import Bank Statement'

    attachment_ids = fields.Many2many('ir.attachment', string='Files', required=True, help='Get you bank statements in electronic format from your bank and select them here.')

    def import_file(self):
        """ Process the file chosen in the wizard, create bank statement(s) and go to reconciliation. """
        self.ensure_one()
        statement_ids_all = []
        statement_line_ids_all = []
        notifications_all = []
        # Let the appropriate implementation module parse the file and return the required data
        # The active_id is passed in context in case an implementation module requires information about the wizard state (see QIF)
        for data_file in self.attachment_ids:
            currency_code, account_number, stmts_vals = self.with_context(active_id=self.ids[0])._parse_file(base64.b64decode(data_file.datas))
            # Check raw data
            self._check_parsed_data(stmts_vals, account_number)
            # Try to find the currency and journal in odoo
            currency, journal = self._find_additional_data(currency_code, account_number)
            # If no journal found, ask the user about creating one
            if not journal:
                # The active_id is passed in context so the wizard can call import_file again once the journal is created
                return self.with_context(active_id=self.ids[0])._journal_creation_wizard(currency, account_number)
            if not journal.default_account_id:
                raise UserError(_('You have to set a Default Account for the journal: %s') % (journal.name,))
            # Prepare statement data to be used for bank statements creation
            stmts_vals = self._complete_stmts_vals(stmts_vals, journal, account_number)
            # Create the bank statements
            statement_ids, statement_line_ids, notifications = self._create_bank_statements(stmts_vals)
            statement_ids_all.extend(statement_ids)
            statement_line_ids_all.extend(statement_line_ids)
            notifications_all.extend(notifications)

            # Now that the import worked out, set it as the bank_statements_source of the journal
            if journal.bank_statements_source != 'file_import':
                # Use sudo() because only 'account.group_account_manager'
                # has write access on 'account.journal', but 'account.group_account_user'
                # must be able to import bank statement files
                journal.sudo().bank_statements_source = 'file_import'

            # Post the warnings on the statements
            msg = ""
            for notif in notifications:
                msg += (
                    f"{notif['message']}<br/><br/>"
                    f"{notif['details']['name']}<br/>"
                    f"{notif['details']['model']}<br/>"
                    f"{notif['details']['ids']}<br/><br/>"
                )
            if msg:
                statements = self.env['account.bank.statement'].browse(statement_ids)
                for statement in statements:
                    statement.message_post(body=msg)

        statements = self.env['account.bank.statement'].browse(statement_ids_all)
        # Dispatch to reconciliation interface if all statements are posted.
        if all(s.state == 'posted' for s in statements):
            return {
                'type': 'ir.actions.client',
                'tag': 'bank_statement_reconciliation_view',
                'context': {'statement_line_ids': statement_line_ids_all,
                            'company_ids': self.env.user.company_ids.ids,
                            'notifications': notifications_all,
                },
            }

        # Dispatch to the statemtent list/form view instead.
        result = self.env['ir.actions.act_window']._for_xml_id('account.action_bank_statement_tree')
        if len(statements) > 1:
            result['domain'] = [('id', 'in', statements.ids)]
        elif len(statements) == 1:
            form = self.env.ref('account.view_bank_statement_form', False)
            form_view = [(form.id if form else False, 'form')]
            if 'views' in result:
                result['views'] = form_view + [(state, view) for state, view in result['views'] if view != 'form']
            else:
                result['views'] = form_view
            result['res_id'] = statements.id
        else:
            result = {'type': 'ir.actions.act_window_close'}
        return result

    def _journal_creation_wizard(self, currency, account_number):
        """ Calls a wizard that allows the user to carry on with journal creation """
        return {
            'name': _('Journal Creation'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.bank.statement.import.journal.creation',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'statement_import_transient_id': self.env.context['active_id'],
                'default_bank_acc_number': account_number,
                'default_name': _('Bank') + ' ' + account_number,
                'default_currency_id': currency and currency.id or False,
                'default_type': 'bank',
            }
        }

    def _parse_file(self, data_file):
        """ Each module adding a file support must extends this method. It processes the file if it can, returns super otherwise, resulting in a chain of responsability.
            This method parses the given file and returns the data required by the bank statement import process, as specified below.
            rtype: triplet (if a value can't be retrieved, use None)
                - currency code: string (e.g: 'EUR')
                    The ISO 4217 currency code, case insensitive
                - account number: string (e.g: 'BE1234567890')
                    The number of the bank account which the statement belongs to
                - bank statements data: list of dict containing (optional items marked by o) :
                    - 'name': string (e.g: '000000123')
                    - 'date': date (e.g: 2013-06-26)
                    -o 'balance_start': float (e.g: 8368.56)
                    -o 'balance_end_real': float (e.g: 8888.88)
                    - 'transactions': list of dict containing :
                        - 'name': string (e.g: 'KBC-INVESTERINGSKREDIET 787-5562831-01')
                        - 'date': date
                        - 'amount': float
                        - 'unique_import_id': string
                        -o 'account_number': string
                            Will be used to find/create the res.partner.bank in odoo
                        -o 'note': string
                        -o 'partner_name': string
                        -o 'ref': string
        """
        raise UserError(_('Could not make sense of the given file.\nDid you install the module to support this type of file ?'))

    def _check_parsed_data(self, stmts_vals, account_number):
        """ Basic and structural verifications """
        extra_msg = _('If it contains transactions for more than one account, it must be imported on each of them.')
        if len(stmts_vals) == 0:
            raise UserError(
                _('This file doesn\'t contain any statement for account %s.') % (account_number,)
                + '\n' + extra_msg
            )

        no_st_line = True
        for vals in stmts_vals:
            if vals['transactions'] and len(vals['transactions']) > 0:
                no_st_line = False
                break
        if no_st_line:
            raise UserError(
                _('This file doesn\'t contain any transaction for account %s.') % (account_number,)
                + '\n' + extra_msg
            )

    def _check_journal_bank_account(self, journal, account_number):
        # Needed for CH to accommodate for non-unique account numbers
        sanitized_acc_number = journal.bank_account_id.sanitized_acc_number.split(" ")[0]
        # Needed for BNP France
        if len(sanitized_acc_number) == 27 and len(account_number) == 11 and sanitized_acc_number[:2].upper() == "FR":
            return sanitized_acc_number[14:-2] == account_number

        # Needed for Credit Lyonnais (LCL)
        if len(sanitized_acc_number) == 27 and len(account_number) == 7 and sanitized_acc_number[:2].upper() == "FR":
            return sanitized_acc_number[18:-2] == account_number

        return sanitized_acc_number == account_number

    def _find_additional_data(self, currency_code, account_number):
        """ Look for a res.currency and account.journal using values extracted from the
            statement and make sure it's consistent.
        """
        company_currency = self.env.company.currency_id
        journal_obj = self.env['account.journal']
        currency = None
        sanitized_account_number = sanitize_account_number(account_number)

        if currency_code:
            currency = self.env['res.currency'].search([('name', '=ilike', currency_code)], limit=1)
            if not currency:
                raise UserError(_("No currency found matching '%s'.", currency_code))
            if currency == company_currency:
                currency = False

        journal = journal_obj.browse(self.env.context.get('journal_id', []))
        if account_number:
            # No bank account on the journal : create one from the account number of the statement
            if journal and not journal.bank_account_id:
                journal.set_bank_account(account_number)
            # No journal passed to the wizard : try to find one using the account number of the statement
            elif not journal:
                journal = journal_obj.search([('bank_account_id.sanitized_acc_number', '=', sanitized_account_number)])
                if not journal:
                    # Sometimes the bank returns only part of the full account number (e.g. local account number instead of full IBAN)
                    partial_match = journal_obj.search([('bank_account_id.sanitized_acc_number', 'ilike', sanitized_account_number)])
                    if len(partial_match) == 1:
                        journal = partial_match
            # Already a bank account on the journal : check it's the same as on the statement
            else:
                if not self._check_journal_bank_account(journal, sanitized_account_number):
                    raise UserError(_('The account of this statement (%s) is not the same as the journal (%s).') % (account_number, journal.bank_account_id.acc_number))

        # If importing into an existing journal, its currency must be the same as the bank statement
        if journal:
            journal_currency = journal.currency_id
            if currency is None:
                currency = journal_currency
            if currency and currency != journal_currency:
                statement_cur_code = not currency and company_currency.name or currency.name
                journal_cur_code = not journal_currency and company_currency.name or journal_currency.name
                raise UserError(_('The currency of the bank statement (%s) is not the same as the currency of the journal (%s).') % (statement_cur_code, journal_cur_code))

        # If we couldn't find / can't create a journal, everything is lost
        if not journal and not account_number:
            raise UserError(_('Cannot find in which journal import this statement. Please manually select a journal.'))

        return currency, journal

    def _complete_stmts_vals(self, stmts_vals, journal, account_number):
        for st_vals in stmts_vals:
            st_vals['journal_id'] = journal.id
            if not st_vals.get('reference'):
                st_vals['reference'] = " ".join(self.attachment_ids.mapped('name'))
            for line_vals in st_vals['transactions']:
                unique_import_id = line_vals.get('unique_import_id')
                if unique_import_id:
                    sanitized_account_number = sanitize_account_number(account_number)
                    line_vals['unique_import_id'] = (sanitized_account_number and sanitized_account_number + '-' or '') + str(journal.id) + '-' + unique_import_id

                if not line_vals.get('partner_bank_id'):
                    # Find the partner and his bank account or create the bank account. The partner selected during the
                    # reconciliation process will be linked to the bank when the statement is closed.
                    identifying_string = line_vals.get('account_number')
                    if identifying_string:
                        partner_bank = self.env['res.partner.bank']\
                            .search([('acc_number', '=', identifying_string), ('company_id', 'in', (False, journal.company_id.id))], limit=1)
                        if partner_bank:
                            line_vals['partner_bank_id'] = partner_bank.id
                            line_vals['partner_id'] = partner_bank.partner_id.id
        return stmts_vals

    def _create_bank_statements(self, stmts_vals):
        """ Create new bank statements from imported values, filtering out already imported transactions, and returns data used by the reconciliation widget """
        BankStatement = self.env['account.bank.statement']
        BankStatementLine = self.env['account.bank.statement.line']

        # Filter out already imported transactions and create statements
        statement_ids = []
        statement_line_ids = []
        ignored_statement_lines_import_ids = []
        for st_vals in stmts_vals:
            filtered_st_lines = []
            for line_vals in st_vals['transactions']:
                if (line_vals['amount'] != 0
                   and ('unique_import_id' not in line_vals
                   or not line_vals['unique_import_id']
                   or not bool(BankStatementLine.sudo().search([('unique_import_id', '=', line_vals['unique_import_id'])], limit=1)))):
                    filtered_st_lines.append(line_vals)
                else:
                    ignored_statement_lines_import_ids.append(line_vals['unique_import_id'])
                    if st_vals.get('balance_start') is not None:
                        st_vals['balance_start'] += float(line_vals['amount'])

            if len(filtered_st_lines) > 0:
                # Remove values that won't be used to create records
                st_vals.pop('transactions', None)
                number = st_vals.pop('number', None)
                # Create the statement
                st_vals['line_ids'] = [[0, False, line] for line in filtered_st_lines]
                statement = BankStatement.create(st_vals)
                statement_ids.append(statement.id)
                if number and number.isdecimal():
                    statement._set_next_sequence()
                    format, format_values = statement._get_sequence_format_param(statement.name)
                    format_values['seq'] = int(number)
                    #build the full name like BNK/2016/00135 by just giving the number '135'
                    statement.name = format.format(**format_values)
                if statement.balance_end == statement.balance_end_real:
                    statement.button_post()
                statement_line_ids.extend(statement.line_ids.ids)
        if len(statement_line_ids) == 0:
            raise UserError(_('You already have imported that file.'))

        # Prepare import feedback
        notifications = []
        num_ignored = len(ignored_statement_lines_import_ids)
        if num_ignored > 0:
            notifications += [{
                'type': 'warning',
                'message': _("%d transactions had already been imported and were ignored.", num_ignored)
                           if num_ignored > 1
                           else _("1 transaction had already been imported and was ignored."),
                'details': {
                    'name': _('Already imported items'),
                    'model': 'account.bank.statement.line',
                    'ids': BankStatementLine.search([('unique_import_id', 'in', ignored_statement_lines_import_ids)]).ids
                }
            }]
        return statement_ids, statement_line_ids, notifications
