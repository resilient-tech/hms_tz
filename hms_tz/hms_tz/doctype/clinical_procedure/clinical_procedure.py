# -*- coding: utf-8 -*-
# Copyright (c) 2017, ESS LLP and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, nowdate, nowtime, cstr, to_timedelta
import datetime
from erpnext.healthcare.doctype.healthcare_settings.healthcare_settings import get_account
from hms_tz.hms_tz.doctype.lab_test.lab_test import create_sample_doc
from erpnext.stock.stock_ledger import get_previous_sle
from erpnext.stock.get_item_details import get_item_details
from frappe.model.mapper import get_mapped_doc


class ClinicalProcedure(Document):
    def validate(self):
        self.set_status()
        self.set_title()
        if self.consume_stock:
            self.set_actual_qty()

        if self.items:
            self.invoice_separately_as_consumables = False
            for item in self.items:
                if item.invoice_separately_as_consumables:
                    self.invoice_separately_as_consumables = True

    def before_insert(self):
        if self.consume_stock:
            self.set_actual_qty()
        set_procedure_pre_post_tasks(self)

    def after_insert(self):
        if self.prescription:
            frappe.db.set_value('Procedure Prescription',
                                self.prescription, 'procedure_created', 1)
            frappe.db.set_value('Procedure Prescription',
                                self.prescription, 'clinical_procedure', self.name)
        if self.appointment:
            frappe.db.set_value('Patient Appointment',
                                self.appointment, 'status', 'Closed')
        template = frappe.get_doc(
            'Clinical Procedure Template', self.procedure_template)
        if template.sample:
            patient = frappe.get_doc('Patient', self.patient)
            sample_collection = create_sample_doc(
                template, patient, None, self.company)
            frappe.db.set_value('Clinical Procedure', self.name,
                                'sample', sample_collection.name)
        create_pre_post_document(self)
        self.reload()

    def on_trash(self):
        delete_pre_post_documents(self)

    def on_submit(self):
        make_insurance_claim(self)

    def set_status(self):
        if self.docstatus == 0:
            self.status = 'Draft'
        elif self.docstatus == 1:
            if self.status not in ['In Progress', 'Completed']:
                self.status = 'Pending'
        elif self.docstatus == 2:
            self.status = 'Cancelled'

    def set_title(self):
        self.title = _(
            '{0} - {1}').format(self.patient_name or self.patient, self.procedure_template)[:100]

    def complete_procedure(self):
        if self.consume_stock and self.items:
            stock_entry = make_stock_entry(self)

        if self.items:
            consumable_total_amount = 0
            consumption_details = False
            customer = frappe.db.get_value('Patient', self.patient, 'customer')
            if customer:
                for item in self.items:
                    if item.invoice_separately_as_consumables:
                        price_list, price_list_currency = frappe.db.get_values(
                            'Price List', {'selling': 1}, ['name', 'currency'])[0]
                        args = {
                            'doctype': 'Sales Invoice',
                            'item_code': item.item_code,
                            'company': self.company,
                            'warehouse': self.warehouse,
                            'customer': customer,
                            'selling_price_list': price_list,
                            'price_list_currency': price_list_currency,
                            'plc_conversion_rate': 1.0,
                            'conversion_rate': 1.0
                        }
                        item_details = get_item_details(args)
                        item_price = item_details.price_list_rate * item.qty
                        item_consumption_details = item_details.item_name + ' ' + \
                            str(item.qty) + ' ' + item.uom + \
                            ' ' + str(item_price)
                        consumable_total_amount += item_price
                        if not consumption_details:
                            consumption_details = _(
                                'Clinical Procedure ({0}):').format(self.name)
                        consumption_details += '\n\t' + item_consumption_details

                if consumable_total_amount > 0:
                    frappe.db.set_value(
                        'Clinical Procedure', self.name, 'consumable_total_amount', consumable_total_amount)
                    frappe.db.set_value(
                        'Clinical Procedure', self.name, 'consumption_details', consumption_details)
            else:
                frappe.throw(_('Please set Customer in Patient {0}').format(
                    frappe.bold(self.patient)), title=_('Customer Not Found'))

        self.db_set('status', 'Completed')

        if self.consume_stock and self.items:
            return stock_entry

    def start_procedure(self):
        allow_start = self.set_actual_qty()
        if allow_start:
            self.db_set('status', 'In Progress')
            insert_clinical_procedure_to_medical_record(self)
            return 'success'
        return 'insufficient stock'

    def set_actual_qty(self):
        allow_negative_stock = frappe.db.get_single_value(
            'Stock Settings', 'allow_negative_stock')

        allow_start = True
        for d in self.get('items'):
            d.actual_qty = get_stock_qty(d.item_code, self.warehouse)
            # validate qty
            if not allow_negative_stock and d.actual_qty < d.qty:
                allow_start = False
                break

        return allow_start

    def make_material_receipt(self, submit=False):
        stock_entry = frappe.new_doc('Stock Entry')

        stock_entry.stock_entry_type = 'Material Receipt'
        stock_entry.to_warehouse = self.warehouse
        expense_account = get_account(
            None, 'expense_account', 'Healthcare Settings', self.company)
        for item in self.items:
            if item.qty > item.actual_qty:
                se_child = stock_entry.append('items')
                se_child.item_code = item.item_code
                se_child.item_name = item.item_name
                se_child.uom = item.uom
                se_child.stock_uom = item.stock_uom
                se_child.qty = flt(item.qty - item.actual_qty)
                se_child.t_warehouse = self.warehouse
                # in stock uom
                se_child.transfer_qty = flt(item.transfer_qty)
                se_child.conversion_factor = flt(item.conversion_factor)
                cost_center = frappe.get_cached_value(
                    'Company',  self.company,  'cost_center')
                se_child.cost_center = cost_center
                se_child.expense_account = expense_account
        if submit:
            stock_entry.submit()
            return stock_entry
        return stock_entry.as_dict()


def get_stock_qty(item_code, warehouse):
    return get_previous_sle({
        'item_code': item_code,
        'warehouse': warehouse,
        'posting_date': nowdate(),
        'posting_time': nowtime()
    }).get('qty_after_transaction') or 0


def set_procedure_pre_post_tasks(doc):
    if doc.procedure_template:
        template = frappe.get_doc(
            "Clinical Procedure Template", doc.procedure_template)
        if template.nursing_tasks:
            for nursing_task in template.nursing_tasks:
                nursing_task_item = doc.append("nursing_tasks")
                nursing_task_item.check_list = nursing_task.check_list
                nursing_task_item.task = nursing_task.task
                nursing_task_item.expected_time = nursing_task.expected_time


def create_pre_post_document(doc):
    if doc.nursing_tasks:
        for nursing_task in doc.nursing_tasks:
            if not nursing_task.nursing_task_reference:
                create_nursing_task(doc, nursing_task)


def delete_pre_post_documents(doc):
    if doc.nursing_tasks:
        for nursing_task in frappe.get_list('Healthcare Nursing Task', {'reference_doctype': doc.doctype, 'reference_docname': doc.name}):
            try:
                frappe.delete_doc("Healthcare Nursing Task", nursing_task.name)
            except Exception:
                pass


def create_nursing_task(doc, nursing_task):
    hc_nursing_task = frappe.new_doc("Healthcare Nursing Task")
    hc_nursing_task.patient = doc.patient
    hc_nursing_task.practitioner = doc.practitioner
    hc_nursing_task.task = nursing_task.check_list
    hc_nursing_task.service_unit = doc.service_unit
    hc_nursing_task.medical_department = doc.medical_department
    hc_nursing_task.task_order = nursing_task.task
    hc_nursing_task.reference_doctype = doc.doctype
    hc_nursing_task.reference_docname = doc.name
    hc_nursing_task.date = doc.start_date
    # if doc.start_time and nursing_task.expected_time and nursing_task.expected_time > 0:
    #     if nursing_task.task == "After":
    #         hc_nursing_task.time = to_timedelta(
    #             doc.start_time) + datetime.timedelta(seconds=nursing_task.expected_time*60)
    #     elif nursing_task.task   == "Before":
    #         hc_nursing_task.time = to_timedelta(
    #             doc.start_time) - datetime.timedelta(seconds=nursing_task.expected_time*60)
    hc_nursing_task.save(ignore_permissions=True)


@frappe.whitelist()
def get_procedure_consumables(procedure_template):
    return get_items('Clinical Procedure Item', procedure_template, 'Clinical Procedure Template')


@frappe.whitelist()
def set_stock_items(doc, stock_detail_parent, parenttype):
    items = get_items('Clinical Procedure Item',
                      stock_detail_parent, parenttype)

    for item in items:
        se_child = doc.append('items')
        se_child.item_code = item.item_code
        se_child.item_name = item.item_name
        se_child.uom = item.uom
        se_child.stock_uom = item.stock_uom
        se_child.qty = flt(item.qty)
        # in stock uom
        se_child.transfer_qty = flt(item.transfer_qty)
        se_child.conversion_factor = flt(item.conversion_factor)
        if item.batch_no:
            se_child.batch_no = item.batch_no
        if parenttype == 'Clinical Procedure Template':
            se_child.invoice_separately_as_consumables = item.invoice_separately_as_consumables

    return doc


def get_items(table, parent, parenttype):
    items = frappe.db.get_all(table, filters={
        'parent': parent,
        'parenttype': parenttype
    }, fields=['*'])

    return items


@frappe.whitelist()
def make_stock_entry(doc):
    stock_entry = frappe.new_doc('Stock Entry')
    stock_entry = set_stock_items(stock_entry, doc.name, 'Clinical Procedure')
    stock_entry.stock_entry_type = 'Material Issue'
    stock_entry.from_warehouse = doc.warehouse
    stock_entry.company = doc.company
    expense_account = get_account(
        None, 'expense_account', 'Healthcare Settings', doc.company)

    for item_line in stock_entry.items:
        cost_center = frappe.get_cached_value(
            'Company',  doc.company,  'cost_center')
        item_line.cost_center = cost_center
        item_line.expense_account = expense_account

    stock_entry.save(ignore_permissions=True)
    stock_entry.submit()
    return stock_entry.name


@frappe.whitelist()
def make_procedure(source_name, target_doc=None):
    def set_missing_values(source, target):
        consume_stock = frappe.db.get_value(
            'Clinical Procedure Template', source.procedure_template, 'consume_stock')
        if consume_stock:
            target.consume_stock = 1
            warehouse = None
            if source.service_unit:
                warehouse = frappe.db.get_value(
                    'Healthcare Service Unit', source.service_unit, 'warehouse')
            if not warehouse:
                warehouse = frappe.db.get_value(
                    'Stock Settings', None, 'default_warehouse')
            if warehouse:
                target.warehouse = warehouse

            set_stock_items(target, source.procedure_template,
                            'Clinical Procedure Template')

    doc = get_mapped_doc('Patient Appointment', source_name, {
        'Patient Appointment': {
            'doctype': 'Clinical Procedure',
            'field_map': [
                ['appointment', 'name'],
                ['patient', 'patient'],
                ['patient_age', 'patient_age'],
                ['patient_sex', 'patient_sex'],
                ['procedure_template', 'procedure_template'],
                ['prescription', 'procedure_prescription'],
                ['practitioner', 'practitioner'],
                ['medical_department', 'department'],
                ['start_date', 'appointment_date'],
                ['start_time', 'appointment_time'],
                ['notes', 'notes'],
                ['service_unit', 'service_unit'],
                ['company', 'company'],
                ['invoiced', 'invoiced']
            ]
        }
    }, target_doc, set_missing_values)

    return doc


def insert_clinical_procedure_to_medical_record(doc):
    subject = frappe.bold(_("Clinical Procedure conducted: ")) + \
        cstr(doc.procedure_template) + "<br>"
    if doc.practitioner:
        subject += frappe.bold(_('Healthcare Practitioner: ')
                               ) + doc.practitioner
    if subject and doc.notes:
        subject += '<br/>' + doc.notes

    medical_record = frappe.new_doc('Patient Medical Record')
    medical_record.patient = doc.patient
    medical_record.subject = subject
    medical_record.status = 'Open'
    medical_record.communication_date = doc.start_date
    medical_record.reference_doctype = 'Clinical Procedure'
    medical_record.reference_name = doc.name
    medical_record.reference_owner = doc.owner
    medical_record.save(ignore_permissions=True)


def make_insurance_claim(doc):
    if doc.insurance_subscription:
        from hms_tz.hms_tz.utils import create_insurance_claim
        billing_item, = frappe.get_cached_value(
            'Clinical Procedure Template', doc.procedure_template, ['item'])
        insurance_claim, claim_status = create_insurance_claim(
            doc, 'Clinical Procedure Template', doc.procedure_template, 1, billing_item)
        if insurance_claim:
            frappe.set_value(doc.doctype, doc.name,
                             'insurance_claim', insurance_claim)
            frappe.set_value(doc.doctype, doc.name,
                             'claim_status', claim_status)
