# -*- coding: utf-8 -*-
# Copyright (c) 2018, earthians and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from erpnext.healthcare.doctype.healthcare_settings.healthcare_settings import get_income_account
from hms_tz.hms_tz.utils import validate_customer_created
from hms_tz.nhif.api.patient_appointment import get_insurance_amount
from frappe.utils import nowdate, nowtime, add_days
import base64
import re


@frappe.whitelist()
def get_healthcare_services_to_invoice(patient, company, encounter=None, service_order_category=None, prescribed=None):
    patient = frappe.get_doc('Patient', patient)
    items_to_invoice = []
    if patient:
        validate_customer_created(patient)
        # Customer validated, build a list of billable services
        if encounter:
            items_to_invoice += get_healthcare_service_order_to_invoice(
                patient, company, encounter, service_order_category, prescribed)
        return items_to_invoice


def get_healthcare_service_order_to_invoice(patient, company, encounter, service_order_category=None, prescribed=None):
    encounter_dict = frappe.get_all("Patient Encounter", filters={
        "reference_encounter": encounter,
        "docstatus": 1,
        'is_not_billable': 0
    })
    encounter_list = [encounter]

    for i in encounter_dict:
        encounter_list.append(i.name)

    filters = {
        'patient': patient.name,
        'company': company,
        'order_group': ["in", encounter_list],
        'invoiced': 0,
        'order_date': [">", add_days(nowdate(), -3)],
        'docstatus': 1
    }

    if service_order_category:
        filters['healthcare_service_order_category'] = service_order_category
    filters['prescribed'] = 1
    services_to_invoice = []
    services = frappe.get_list(
        'Healthcare Service Order',
        fields=['*'],
        filters=filters
    )

    if services:
        for service in services:
            practitioner_charge = 0
            income_account = None
            service_item = None
            if service.order_doctype and service.order:
                is_not_available_inhouse = frappe.get_value(
                    service.order_doctype, service.order, "is_not_available_inhouse")
                if is_not_available_inhouse:
                    continue
            if service.ordered_by:
                service_item = service.billing_item
                practitioner_charge = get_insurance_amount(
                    service.insurance_subscription, service.billing_item, company, patient.name, service.insurance_company)
                income_account = get_income_account(
                    service.ordered_by, company)

            services_to_invoice.append({
                'reference_type': 'Healthcare Service Order',
                'reference_name': service.name,
                'service': service_item,
                'rate': practitioner_charge,
                'income_account': income_account,
                'qty': service.quantity
            })

    return services_to_invoice


def get_item_price(item_code, price_list, company):
    price = 0
    company_currency = frappe.get_value("Company", company, "default_currency")
    item_prices_data = frappe.get_all("Item Price",
                                      fields=[
                                          "item_code", "price_list_rate", "currency"],
                                      filters={
                                          'price_list': price_list, 'item_code': item_code, 'currency': company_currency},
                                      order_by="valid_from desc"
                                      )
    if len(item_prices_data):
        price = item_prices_data[0].price_list_rate
    return price


def get_item_rate(item_code, company, insurance_subscription, insurance_company):
    price_list = None
    price_list_rate = None
    if insurance_subscription:
        hic_plan = frappe.get_value(
            "Healthcare Insurance Subscription", insurance_subscription, "healthcare_insurance_coverage_plan")
        price_list = frappe.get_value(
            "Healthcare Insurance Coverage Plan", hic_plan, "price_list")
        secondary_price_list = frappe.get_value(
            "Healthcare Insurance Coverage Plan", hic_plan, "secondary_price_list")
        if price_list:
            price_list_rate = get_item_price(item_code, price_list, company)
            if price_list_rate and price_list_rate != 0:
                return price_list_rate
            else:
                price_list_rate = None
        if not price_list_rate:
            price_list_rate = get_item_price(item_code, secondary_price_list, company)
            if price_list_rate and price_list_rate != 0:
                return price_list_rate
            else:
                price_list_rate = None

    if not price_list_rate and insurance_company:
        price_list = frappe.get_value(
            "Healthcare Insurance Company", insurance_company, "default_price_list")
    if not price_list:
        frappe.throw(
            _("Please set Price List in Healthcare Insurance Coverage Plan"))
    else:
        price_list_rate = get_item_price(item_code, price_list, company)
    if price_list_rate == (0 or None):
        frappe.throw(
            _("Please set Price List for item: {0}").format(item_code))
    return price_list_rate


def to_base64(value):
    data = base64.b64encode(value)
    return str(data)[2:-1]


def remove_special_characters(text):
    return re.sub('[^A-Za-z0-9]+', '', text)


def get_app_branch(app):
    '''Returns branch of an app'''
    import subprocess
    try:
        branch = subprocess.check_output('cd ../apps/{0} && git rev-parse --abbrev-ref HEAD'.format(app),
                                         shell=True)
        branch = branch.decode('utf-8')
        branch = branch.strip()
        return branch
    except Exception:
        return ''


def create_delivery_note_from_LRPT(LRPT_doc, patient_encounter_doc):
    if not patient_encounter_doc.appointment:
        return
    insurance_subscription, insurance_company = frappe.get_value(
        "Patient Appointment", patient_encounter_doc.appointment, ["insurance_subscription", "insurance_company"])
    if not insurance_subscription:
        return
    warehouse = get_warehouse_from_service_unit(
        patient_encounter_doc.healthcare_service_unit)
    items = []
    item = get_item_form_LRPT(LRPT_doc)
    item_code = item.get("item_code")
    if not item_code:
        return
    is_stock, item_name = frappe.get_value(
        "Item", item_code, ["is_stock_item", "item_name"])
    if is_stock:
        return
    item_row = frappe.new_doc("Delivery Note Item")
    item_row.item_code = item_code
    item_row.item_name = item_name
    item_row.warehouse = warehouse
    item_row.healthcare_service_unit = item.healthcare_service_unit
    item_row.practitioner = patient_encounter_doc.practitioner
    item_row.qty = item.qty
    item_row.rate = get_item_rate(
        item_code, patient_encounter_doc.company, insurance_subscription, insurance_company)
    item_row.reference_doctype = LRPT_doc.doctype
    item_row.reference_name = LRPT_doc.name
    item_row.description = frappe.get_value("Item", item_code, "description")
    items.append(item_row)

    if len(items) == 0:
        return
    doc = frappe.get_doc(dict(
        doctype="Delivery Note",
        posting_date=nowdate(),
        posting_time=nowtime(),
        set_warehouse=warehouse,
        company=patient_encounter_doc.company,
        customer=frappe.get_value(
            "Healthcare Insurance Company", insurance_company, "customer"),
        currency=frappe.get_value(
            "Company", patient_encounter_doc.company, "default_currency"),
        items=items,
        reference_doctype=LRPT_doc.doctype,
        reference_name=LRPT_doc.name,
        patient=patient_encounter_doc.patient,
        patient_name=patient_encounter_doc.patient_name
    ))
    doc.flags.ignore_permissions = True
    # doc.set_missing_values()
    doc.insert(ignore_permissions = True)
    doc.submit()
    if doc.get('name'):
        frappe.msgprint(_('Delivery Note {0} created successfully.').format(
            frappe.bold(doc.name)), alert=True)


def get_warehouse_from_service_unit(healthcare_service_unit):
    warehouse = frappe.get_value(
        "Healthcare Service Unit", healthcare_service_unit, "warehouse")
    if not warehouse:
        frappe.throw(_("Warehouse is missing in Healthcare Service Unit"))
    return warehouse


def get_item_form_LRPT(LRPT_doc):
    item = frappe._dict()
    if LRPT_doc.doctype == "Lab Test":
        item.item_code, item.healthcare_service_unit = frappe.get_value(
            "Lab Test Template", LRPT_doc.template, ["item", "healthcare_service_unit"])
        item.qty = 1
    elif LRPT_doc.doctype == "Radiology Examination":
        item.item_code, item.healthcare_service_unit = frappe.get_value(
            "Radiology Examination Template", LRPT_doc.radiology_examination_template, ["item", "healthcare_service_unit"])
        item.qty = 1
    elif LRPT_doc.doctype == "Clinical Procedure":
        item.item_code, item.healthcare_service_unit = frappe.get_value(
             "Clinical Procedure Template", LRPT_doc.procedure_template, ["item", "healthcare_service_unit"])
        item.qty = 1
    elif LRPT_doc.doctype == "Therapy Plan":
        item.item_code = None
        item.qty = 0
    return item


def update_dimensions(doc):
    for item in doc.items:
        refd, refn = get_references(item)
        if doc.healthcare_practitioner:
            item.healthcare_practitioner = doc.healthcare_practitioner
        elif refd and refn:
            item.healthcare_practitioner = get_healthcare_practitioner(item)

        if doc.healthcare_service_unit and not item.healthcare_service_unit and not refn:
            item.healthcare_service_unit = doc.healthcare_service_unit

        if refd and refn:
            item.healthcare_service_unit = get_healthcare_service_unit(item)


def get_references(item):
    refd = ""
    refn = ""
    if item.get("reference_doctype"):
        refd = item.get("reference_doctype")
        refn = item.get("reference_name")
    elif item.get("reference_dt"):
        refd = item.get("reference_dt")
        refn = item.get("reference_dn")
    return refd, refn


def get_healthcare_practitioner(item):
    refd, refn = get_references(item)
    if not refd or not refn:
        return
    if refd == "Patient Encounter":
        return frappe.get_value("Patient Encounter", refn, "practitioner")
    elif refd == "Patient Appointment":
        return frappe.get_value("Patient Appointment", refn, "practitioner")
    elif refd == "Drug Prescription":
        parent, parenttype = frappe.get_value("Drug Prescription", refn, [
            "parent", "parenttype"])
        if parenttype == "Patient Encounter":
            return frappe.get_value("Patient Encounter", parent, "practitioner")
    elif refd == "Healthcare Service Order":
        encounter = frappe.get_value(
            "Healthcare Service Order", refn, "order_group")
        if encounter:
            return frappe.get_value("Patient Encounter", encounter, "practitioner")


def get_healthcare_service_unit(item):
    refd, refn = get_references(item)
    if not refd or not refn:
        return
    if refd == "Patient Encounter":
        return frappe.get_value("Patient Encounter", refn, "healthcare_service_unit")
    elif refd == "Patient Appointment":
        return frappe.get_value("Patient Appointment", refn, "service_unit")
    elif refd == "Drug Prescription":
        return frappe.get_value("Drug Prescription", refn, "healthcare_service_unit")
    elif refd == "Healthcare Service Order":
        order_doctype, order, order_group, billing_item = frappe.get_value(
            refd, refn, ["order_doctype", "order", "order_group", "billing_item"])
        if order_doctype in ["Lab Test Template", "Radiology Examination Template", "Clinical Procedure Template", "Therapy Plan Template"]:
            return frappe.get_value(order_doctype, order, "healthcare_service_unit")
        elif order_doctype == "Medication":
            if not order_group:
                return
            prescriptions = frappe.get_all("Drug Prescription",
                                           filters={
                                               "parent": order_group,
                                               "parentfield": "drug_prescription",
                                               "drug_code": billing_item
                                           },
                                           fields=[
                                               "name", "healthcare_service_unit"]
                                           )
            if len(prescriptions) > 0:
                return prescriptions[0].healthcare_service_unit


def get_restricted_LRPT(doc):
    if doc.doctype == "Lab Test":
        template = doc.template
    elif doc.doctype == "Radiology Examination":
        template = doc.radiology_examination_template
    elif doc.doctype == "Clinical Procedure":
        template = doc.procedure_template
    elif doc.doctype == "Therapy Plan":
        template = doc.therapy_plan_template
    else:
        frappe.msgprint(_("Unknown Doctype " + doc.doctype + " found in get_restricted_LRPT. Setup may be missing."))
    is_restricted = 0
    if not template:
        return is_restricted
    if doc.ref_doctype and doc.ref_docname and doc.ref_doctype == "Patient Encounter":
        insurance_subscription = frappe.get_value(
            "Patient Encounter", doc.ref_docname, "insurance_subscription")
        if insurance_subscription:
            healthcare_insurance_coverage_plan = frappe.get_value(
                "Healthcare Insurance Subscription", insurance_subscription, "healthcare_insurance_coverage_plan")
            if healthcare_insurance_coverage_plan:
                insurance_coverages = frappe.get_all(
                    "Healthcare Service Insurance Coverage", filters={"healthcare_service_template": template, "healthcare_insurance_coverage_plan": healthcare_insurance_coverage_plan}, fields=["name", "approval_mandatory_for_claim"])
                if len(insurance_coverages) > 0:
                    is_restricted = insurance_coverages[0].approval_mandatory_for_claim
    return is_restricted

# Sales Invoice Dialog Box for Healthcare Services
@frappe.whitelist()
def set_healthcare_services(doc, checked_values):
    import json
    doc = frappe.get_doc(json.loads(doc))
    checked_values = json.loads(checked_values)
    doc.items = []
    from erpnext.stock.get_item_details import get_item_details
    for checked_item in checked_values:
        item_line = doc.append("items", {})
        price_list, price_list_currency = frappe.db.get_values(
            "Price List", {"selling": 1}, ['name', 'currency'])[0]
        args = {
            'doctype': "Sales Invoice",
            'item_code': checked_item['item'],
            'company': doc.company,
            'customer': frappe.db.get_value("Patient", doc.patient, "customer"),
            'selling_price_list': price_list,
            'price_list_currency': price_list_currency,
            'plc_conversion_rate': 1.0,
            'conversion_rate': 1.0
        }
        item_details = get_item_details(args)
        item_line.item_code = checked_item['item']
        item_line.qty = 1
        if checked_item['qty']:
            item_line.qty = checked_item['qty']
        if checked_item['rate']:
            item_line.rate = checked_item['rate']
        else:
            item_line.rate = item_details.price_list_rate
        item_line.amount = float(item_line.rate) * float(item_line.qty)
        if checked_item['income_account']:
            item_line.income_account = checked_item['income_account']
        if checked_item['dt']:
            item_line.reference_dt = checked_item['dt']
        if checked_item['dn']:
            item_line.reference_dn = checked_item['dn']
        if checked_item['description']:
            item_line.description = checked_item['description']
        hso_doc = frappe.get_doc(item_line.reference_dt, item_line.reference_dn)
        item_line.healthcare_practitioner = hso_doc.ordered_by
        if hso_doc.order_doctype == "Medication":
            item_line.healthcare_service_unit = frappe.get_value(hso_doc.order_reference_doctype, hso_doc.order_reference_name, "healthcare_service_unit")
        else:
            item_line.healthcare_service_unit = frappe.get_value(hso_doc.order_doctype, hso_doc.order, "healthcare_service_unit")
        item_line.warehouse = get_warehouse_from_service_unit(item_line.healthcare_service_unit)
    doc.set_missing_values(for_validate=True)
    doc.save()
    return doc.name

def create_individual_lab_test(source_doc, child):
    if child.lab_test_created == 1:
        return
    ltt_doc = frappe.get_doc("Lab Test Template", child.lab_test_code)
    patient_sex = frappe.get_value("Patient", source_doc.patient, "sex")

    doc = frappe.new_doc('Lab Test')
    doc.patient = source_doc.patient
    doc.patient_sex = patient_sex
    doc.company = source_doc.company
    doc.template = ltt_doc.name
    if source_doc.doctype == "Healthcare Service Order":
        doc.practitioner = source_doc.ordered_by
    else:
        doc.practitioner = source_doc.practitioner
    doc.source = source_doc.source
    if not child.prescribe:
        doc.insurance_subscription = source_doc.insurance_subscription
    doc.ref_doctype = source_doc.doctype
    doc.ref_docname = source_doc.name
    doc.invoiced = 1
    doc.service_comment = (child.medical_code or "No ICD Code") + \
        " : " + (child.lab_test_comment or "No Comment")

    if doc.get('name'):
        frappe.msgprint(_('Lab Test {0} created successfully.').format(
            frappe.bold(doc.name)))
        child.lab_test_created = 1
        child.lab_test = doc.name
        child.db_update()

    doc.save(ignore_permissions=True)
    frappe.db.commit()

def create_individual_radiology_examination(source_doc, child):
    if child.radiology_examination_created == 1:
        return
    doc = frappe.new_doc('Radiology Examination')
    doc.patient = source_doc.patient
    doc.company = source_doc.company
    doc.radiology_examination_template = child.radiology_examination_template
    if source_doc.doctype == "Healthcare Service Order":
        doc.practitioner = source_doc.ordered_by
    else:
        doc.practitioner = source_doc.practitioner
    doc.source = source_doc.source
    if not child.prescribe:
        doc.insurance_subscription = source_doc.insurance_subscription
    doc.medical_department = frappe.get_value(
        "Radiology Examination Template", child.radiology_examination_template, "medical_department")
    doc.ref_doctype = source_doc.doctype
    doc.ref_docname = source_doc.name
    doc.invoiced = 1
    doc.service_comment = (child.medical_code or "No ICD Code") + " : " + \
        (child.radiology_test_comment or "No Comment")

    if doc.get('name'):
        frappe.msgprint(_('Radiology Examination {0} created successfully.').format(
            frappe.bold(doc.name)))
        child.radiology_examination_created = 1
        child.radiology_examination = doc.name
        child.db_update()

    doc.save(ignore_permissions=True)
    frappe.db.commit()

def create_individual_procedure_prescription(source_doc, child):
    if child.procedure_created == 1:
        return
    doc = frappe.new_doc('Clinical Procedure')
    doc.patient = source_doc.patient
    doc.company = source_doc.company
    doc.procedure_template = child.procedure
    if source_doc.doctype == "Healthcare Service Order":
        doc.practitioner = source_doc.ordered_by
    else:
        doc.practitioner = source_doc.practitioner
    doc.source = source_doc.source
    if not child.prescribe:
        doc.insurance_subscription = source_doc.insurance_subscription
    doc.patient_sex = frappe.get_value(
        "Patient", source_doc.patient, "sex")
    doc.medical_department = frappe.get_value(
        "Clinical Procedure Template", child.procedure, "medical_department")
    doc.ref_doctype = source_doc.doctype
    doc.ref_docname = source_doc.name
    doc.invoiced = 1
    doc.service_comment = (child.medical_code or "No ICD Code") + \
        " : " + (child.comments or "No Comment")

    if doc.get('name'):
        frappe.msgprint(_('Clinical Procedure {0} created successfully.').format(
            frappe.bold(doc.name)))
        child.procedure_created = 1
        child.clinical_procedure = doc.name
        child.db_update()
    doc.save(ignore_permissions=True)
    frappe.db.commit()
