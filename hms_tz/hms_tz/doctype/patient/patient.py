# -*- coding: utf-8 -*-
# Copyright (c) 2015, ESS LLP and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, getdate, flt
import dateutil
from frappe.contacts.address_and_contact import load_address_and_contact, delete_contact_and_address
from frappe.model.naming import set_name_by_naming_series
from frappe.utils.nestedset import get_root_of
from erpnext import get_default_currency
from erpnext.healthcare.doctype.healthcare_settings.healthcare_settings import get_receivable_account, get_income_account, send_registration_sms


class Patient(Document):
    def onload(self):
        """Load address and contacts in `__onload`"""
        load_address_and_contact(self)

    def validate(self):
        self.set_full_name()

    def before_insert(self):
        self.set_missing_customer_details()

    def after_insert(self):
        if frappe.db.get_single_value('Healthcare Settings', 'link_customer_to_patient') and not self.customer:
            create_customer(self)
        if frappe.db.get_single_value('Healthcare Settings', 'collect_registration_fee'):
            frappe.db.set_value('Patient', self.name, 'status', 'Disabled')
        else:
            send_registration_sms(self)
        self.reload()  # self.notify_update()

    def on_update(self):
        if self.customer:
            customer = frappe.get_doc('Customer', self.customer)
            if self.customer_group:
                customer.customer_group = self.customer_group
            if self.territory:
                customer.territory = self.territory

            customer.customer_name = self.patient_name
            customer.default_price_list = self.default_price_list
            customer.default_currency = self.default_currency
            customer.language = self.language
            customer.ignore_mandatory = True
            customer.save(ignore_permissions=True)
        else:
            if frappe.db.get_single_value('Healthcare Settings', 'link_customer_to_patient'):
                create_customer(self)
        if self.email and self.invite_user:
            self.add_as_website_user()
            self.reload()
            self.update_web_site_user_contact()
        update_contacts(self)

    def set_full_name(self):
        self.patient_name = ' '.join(
            filter(None, [self.first_name, self.middle_name or "", self.last_name or ""]))

    def set_missing_customer_details(self):
        if not self.customer_group:
            self.customer_group = frappe.db.get_single_value(
                'Selling Settings', 'customer_group') or get_root_of('Customer Group')
        if not self.territory:
            self.territory = frappe.db.get_single_value(
                'Selling Settings', 'territory') or get_root_of('Territory')
        if not self.default_price_list:
            self.default_price_list = frappe.db.get_single_value(
                'Selling Settings', 'selling_price_list')

        if not self.customer_group or not self.territory or not self.default_price_list:
            frappe.msgprint(
                _('Please set defaults for Customer Group, Territory and Selling Price List in Selling Settings'), alert=True)

        if not self.default_currency:
            self.default_currency = get_default_currency()
        if not self.language:
            self.language = frappe.db.get_single_value(
                'System Settings', 'language')

    def add_as_website_user(self):
        if self.email:
            if not frappe.db.exists('User', self.email):
                user = frappe.get_doc({
                    'doctype': 'User',
                    'first_name': self.first_name,
                    'last_name': self.last_name,
                    'email': self.email,
                    'user_type': 'Website User'
                })
                user.flags.ignore_permissions = True
                user.enabled = True
                user.send_welcome_email = True
                user.add_roles('Patient')

    def autoname(self):
        patient_name_by = frappe.db.get_single_value(
            'Healthcare Settings', 'patient_name_by')
        if patient_name_by == 'Patient Name':
            self.name = self.get_patient_name()
        else:
            set_name_by_naming_series(self)

    def get_patient_name(self):
        self.set_full_name()
        name = self.patient_name
        if frappe.db.get_value('Patient', name):
            count = frappe.db.sql("""select ifnull(MAX(CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED)), 0) from tabPatient
				 where name like %s""", "%{0} - %".format(name), as_list=1)[0][0]
            count = cint(count) + 1
            return "{0} - {1}".format(name, cstr(count))

        return name

    def get_age(self):
        age_str = ''
        if self.dob:
            dob = getdate(self.dob)
            age = dateutil.relativedelta.relativedelta(getdate(), dob)
            age_str = str(age.years) + ' year(s) ' + str(age.months) + \
                ' month(s) ' + str(age.days) + ' day(s)'
        return age_str

    def invoice_patient_registration(self):
        if frappe.db.get_single_value('Healthcare Settings', 'registration_fee'):
            company = frappe.defaults.get_user_default('company')
            if not company:
                company = frappe.db.get_single_value(
                    'Global Defaults', 'default_company')

            sales_invoice = make_invoice(self.name, company)
            sales_invoice.save(ignore_permissions=True)
            frappe.db.set_value('Patient', self.name, 'status', 'Active')
            send_registration_sms(self)

            return {'invoice': sales_invoice.name}

    def get_billing_info(self):
        if not frappe.defaults.get_user_default("Company"):
            return
        return get_patient_billing_info(self)

    def update_web_site_user_contact(self):
        user = frappe.db.exists('User', self.email)
        if user:
            contact_name = frappe.db.exists('Contact', {'user': user})
            if contact_name:
                contact = frappe.get_doc('Contact', contact_name)
                if contact:
                    contact.is_primary_contact = True
                    contact.append('links', dict(
                        link_doctype='Customer', link_name=self.customer))
                    contact.append('links', dict(
                        link_doctype='Patient', link_name=self.name))
                    contact.save(ignore_permissions=True)


def create_customer(doc):
    customer = frappe.get_doc({
        'doctype': 'Customer',
        'customer_name': doc.patient_name,
        'customer_group': doc.customer_group or frappe.db.get_single_value('Selling Settings', 'customer_group'),
        'territory': doc.territory or frappe.db.get_single_value('Selling Settings', 'territory'),
        'customer_type': 'Individual',
        'default_currency': doc.default_currency,
        'default_price_list': doc.default_price_list,
        'language': doc.language
    }).insert(ignore_permissions=True, ignore_mandatory=True)

    frappe.db.set_value('Patient', doc.name, 'customer', customer.name)
    frappe.msgprint(_('Customer {0} is created.').format(
        customer.name), alert=True)


def make_invoice(patient, company):
    uom = frappe.db.exists('UOM', 'Nos') or frappe.db.get_single_value(
        'Stock Settings', 'stock_uom')
    sales_invoice = frappe.new_doc('Sales Invoice')
    sales_invoice.customer = frappe.db.get_value(
        'Patient', patient, 'customer')
    sales_invoice.due_date = getdate()
    sales_invoice.company = company
    sales_invoice.is_pos = 0
    sales_invoice.debit_to = get_receivable_account(company)

    item_line = sales_invoice.append('items')
    item_line.item_name = 'Registeration Fee'
    item_line.description = 'Registeration Fee'
    item_line.qty = 1
    item_line.uom = uom
    item_line.conversion_factor = 1
    item_line.income_account = get_income_account(None, company)
    item_line.rate = frappe.db.get_single_value(
        'Healthcare Settings', 'registration_fee')
    item_line.amount = item_line.rate
    sales_invoice.set_missing_values()
    return sales_invoice


@frappe.whitelist()
def get_patient_detail(patient):
    patient_dict = frappe.db.sql(
        """select * from tabPatient where name=%s""", (patient), as_dict=1)
    if not patient_dict:
        frappe.throw(_('Patient not found'))
    vital_sign = frappe.db.sql("""select * from `tabVital Signs` where patient=%s
		order by signs_date desc limit 1""", (patient), as_dict=1)

    details = patient_dict[0]
    if vital_sign:
        details.update(vital_sign[0])
    return details


def get_patient_billing_info(patient, ip_billing_info=False):
    from erpnext.accounts.utils import get_balance_on
    filters = {'docstatus': 1, 'customer': patient.customer}

    company = False
    if ip_billing_info and patient.inpatient_record:
        filters['inpatient_record'] = patient.inpatient_record
        company = frappe.db.get_value(
            'Inpatient Record', patient.inpatient_record, 'company')

    fields = ['patient', 'sum(grand_total) as grand_total',
              'sum(base_grand_total) as base_grand_total']
    patient_grand_total = frappe.get_all(
        'Sales Invoice', filters=filters, fields=fields)

    filters['status'] = ['not in', 'Paid']
    fields = ['patient', 'sum(outstanding_amount) as outstanding_amount']
    patient_total_unpaid = frappe.get_all(
        'Sales Invoice', filters=filters, fields=fields)

    if not company:
        company = frappe.defaults.get_user_default('company')
    if not company:
        company = frappe.db.get_single_value(
            'Global Defaults', 'default_company')

    company_default_currency = frappe.db.get_value(
        'Company', company, 'default_currency')
    from erpnext.accounts.party import get_party_account_currency
    party_account_currency = get_party_account_currency(
        'Customer', patient.customer, company)

    if party_account_currency == company_default_currency:
        billing_this_year = flt(patient_grand_total[0]['base_grand_total'])
    else:
        billing_this_year = flt(patient_grand_total[0]['grand_total'])
    total_unpaid = flt(patient_total_unpaid[0]['outstanding_amount'])

    info = {}
    info['total_billing'] = flt(billing_this_year) if billing_this_year else 0
    info['currency'] = party_account_currency
    info['total_unpaid'] = flt(total_unpaid) if total_unpaid else 0
    info['party_balance'] = get_balance_on(
        party_type='Customer', party=patient.customer)
    return info


def get_timeline_data(doctype, name):
    """Return timeline data from medical records"""
    return dict(frappe.db.sql('''
		SELECT
			unix_timestamp(communication_date), count(*)
		FROM
			`tabPatient Medical Record`
		WHERE
			patient=%s
			and `communication_date` > date_sub(curdate(), interval 1 year)
		GROUP BY communication_date''', name))


def update_contacts(doc):
    # contact
    existing_contacts = frappe.get_all('Dynamic Link', filters={
        "parenttype": "Contact",
        "link_doctype": "Patient",
        "link_name": doc.name
    }, fields=["parent as name"])
    if not existing_contacts and doc.mobile:
        make_contact(doc)

    if existing_contacts:
        for contact_name in existing_contacts:
            contact = frappe.get_doc('Contact', contact_name.get('name'))
            contact.is_primary_contact = True
            if doc.get('email_id'):
                contact.add_email(doc.get('email_id'), is_primary=True)
            if doc.get('phone'):
                if len(contact.phone_nos) > 0:
                    primary_phone = [
                        phone.phone for phone in contact.phone_nos if phone.get('is_primary_phone')]
                    if primary_phone and primary_phone[0] != doc.get('phone'):
                        update_primary_phone(contact, 'is_primary_phone')
                        contact.add_phone(doc.get('phone'),
                                          is_primary_phone=True)
                    else:
                        contact.add_phone(doc.get('phone'),
                                          is_primary_phone=True)
                else:
                    contact.add_phone(doc.get('phone'), is_primary_phone=True)
            if doc.get('mobile'):
                if len(contact.phone_nos) > 0:
                    primary_phone = [phone.phone for phone in contact.phone_nos if phone.get(
                        'is_primary_mobile_no')]
                    if primary_phone and primary_phone[0] != doc.get('mobile'):
                        update_primary_phone(contact, 'is_primary_mobile_no')
                        contact.add_phone(doc.get('mobile'),
                                          is_primary_mobile_no=True)
                    else:
                        contact.add_phone(doc.get('mobile'),
                                          is_primary_mobile_no=True)
                else:
                    contact.add_phone(doc.get('mobile'),
                                      is_primary_mobile_no=True)
            if not contact.has_link('Customer', doc.customer):
                contact.append('links', dict(
                    link_doctype='Customer', link_name=doc.customer))
            contact.save()


def make_contact(doc):
    contact = frappe.get_doc({
        'doctype': 'Contact',
        'first_name': doc.get('name'),
        'is_primary_contact': True,
    })
    contact.append('links', dict(
        link_doctype='Customer', link_name=doc.customer))
    contact.append('links', dict(link_doctype='Patient', link_name=doc.name))
    if doc.get('email_id'):
        contact.add_email(doc.get('email_id'), is_primary=True)
    if doc.get('mobile'):
        contact.add_phone(doc.get('mobile'), is_primary_mobile_no=True)
    contact.insert()


def update_primary_phone(doc, field_name):
    if doc.phone_nos and len(doc.phone_nos) > 1:
        name = [phone.name for phone in doc.phone_nos if phone.get(field_name)]
        if name:
            frappe.db.set_value('Contact Phone', name[0], field_name, 'False')
            doc.reload()
