# -*- coding: utf-8 -*-
# Copyright (c) 2021, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.utils import nowdate, nowtime
from hms_tz.nhif.api.healthcare_utils import get_item_rate, get_item_price
import json


def validate(doc, method):
    validate_inpatient_occupancies(doc)


def validate_inpatient_occupancies(doc):
    if doc.is_new():
        return
    old_doc = frappe.get_doc(doc.doctype, doc.name)
    count = 0
    for old_row in old_doc.inpatient_occupancies:
        count += 1
        if not old_row.invoiced:
            continue
        valide = True
        row = doc.inpatient_occupancies[count-1]
        if str(row.check_in) != str(old_row.check_in):
            valide = False
        if str(row.check_out) != str(old_row.check_out):
            valide = False
        if row.left != old_row.left:
            valide = False
        if row.service_unit != old_row.service_unit:
            valide = False
        if not valide:
            frappe.throw(
                _("In Inpatient Occupancy line '{0}' has been invoiced. It cannot be modified or deleted").format(old_row.idx))


def daily_update_inpatient_occupancies():
    occupancies = frappe.get_all(
        "Inpatient Record", filters={"status": "Admitted"})

    for item in occupancies:
        doc = frappe.get_doc("Inpatient Record", item.name)
        occupancies_len = len(doc.inpatient_occupancies)
        if occupancies_len > 0:
            last_row = doc.inpatient_occupancies[occupancies_len-1]
            if not last_row.left:
                last_row.left = 1
                last_row.check_out = nowdate()
                new_row = doc.append('inpatient_occupancies', {})
                new_row.check_in = nowdate()
                new_row.left = 0
                new_row.service_unit = last_row.service_unit
                doc.save(ignore_permissions=True)
                frappe.db.commit()


@frappe.whitelist()
def confirmed(row, doc):
    row = frappe._dict(json.loads(row))
    doc = frappe._dict(json.loads(doc))
    if row.invoiced or not row.left:
        return
    encounter = frappe.get_doc("Patient Encounter", doc.admission_encounter)
    service_unit_type, warehouse = frappe.get_value(
        "Healthcare Service Unit", row.service_unit, ["service_unit_type", "warehouse"])
    item_code = frappe.get_value(
        "Healthcare Service Unit Type", service_unit_type, "item_code")
    item_rate = 0
    if encounter.insurance_subscription:
        item_rate = get_item_rate(
            item_code, encounter.company, encounter.insurance_subscription, encounter.insurance_company)
        if not item_rate:
            frappe.throw(_("There is no price in Insurance Subscription {0} for item {1}").format(
                encounter.insurance_subscription, item_code))
    elif encounter.mode_of_payment:
        price_list = frappe.get_value(
            "Mode of Payment", encounter.mode_of_payment, "price_list")
        if not price_list:
            frappe.throw(_("There is no in mode of payment {0}").format(
                encounter.mode_of_payment))
        if price_list:
            item_rate = get_item_price(
                item_code, price_list, encounter.company)
            if not item_rate:
                frappe.throw(_("There is no price in price list {0} for item {1}").format(
                    price_list, item_code))

    if item_rate:
        delivery_note = create_delivery_note(encounter, item_code, item_rate, warehouse,
                                             row, doc.primary_practitioner)
        frappe.set_value(row.doctype, row.name, "invoiced", 1)
        return delivery_note


def create_delivery_note(encounter, item_code, item_rate, warehouse, row, practitioner):
    insurance_subscription = encounter.insurance_subscription
    insurance_company = encounter.insurance_company
    if not insurance_subscription:
        return

    is_stock = frappe.get_value("Item", item_code, "is_stock_item")
    if not is_stock:
        return
    items = []
    item = frappe.new_doc("Delivery Note Item")
    item.item_code = item_code
    # item.item_name = item_name
    item.warehouse = warehouse
    item.qty = 1
    item.rate = item_rate
    item.reference_doctype = row.doctype
    item.reference_name = row.name
    item.description = "For Inpatient Record {0}".format(row.parent)
    items.append(item)

    doc = frappe.get_doc(dict(
        doctype="Delivery Note",
        posting_date=nowdate(),
        posting_time=nowtime(),
        set_warehouse=warehouse,
        company=encounter.company,
        customer=frappe.get_value(
            "Healthcare Insurance Company", insurance_company, "customer"),
        currency=frappe.get_value(
            "Company", encounter.company, "default_currency"),
        items=items,
        reference_doctype=row.parenttype,
        reference_name=row.parent,
        patient=encounter.patient,
        patient_name=encounter.patient_name,
        healthcare_service_unit=row.service_unit,
        healthcare_practitioner=practitioner
    ))
    doc.flags.ignore_permissions = True
    doc.set_missing_values()
    doc.insert(ignore_permissions=True)
    doc.submit()
    if doc.get('name'):
        frappe.msgprint(_('Delivery Note {0} created successfully.').format(
            frappe.bold(doc.name)))
        return doc.get('name')