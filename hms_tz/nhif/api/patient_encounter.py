# -*- coding: utf-8 -*-
# Copyright (c) 2020, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.utils import nowdate, get_year_start, getdate, nowtime, add_to_date
import datetime
from hms_tz.nhif.api.healthcare_utils import get_item_rate, get_warehouse_from_service_unit, get_item_price, create_individual_lab_test, create_individual_radiology_examination, create_individual_procedure_prescription
from erpnext.healthcare.doctype.healthcare_settings.healthcare_settings import get_receivable_account, get_income_account
import time


def after_insert(doc, method):
    doc.sales_invoice = ""
    doc.save()
    doc.reload()


def on_trash(doc, method):
    pmr_list = frappe.get_all("Patient Medical Record",
                              fields={
                                  "name"
                              },
                              filters={
                                  "reference_doctype": "Patient Encounter",
                                  "reference_name": doc.name,
                              },
                              )
    for pmr_doc in pmr_list:
        frappe.delete_doc("Patient Medical Record",
                          pmr_doc.name, ignore_permissions=True)


def validate(doc, method):
    if doc.encounter_type == "Initial":
        doc.reference_encounter = doc.name
    checkـforـduplicate(doc)
    mtuha_missing = ""
    for final_diagnosis in doc.patient_encounter_final_diagnosis:
        if not final_diagnosis.mtuha:
            mtuha_missing += "-  <b>" + final_diagnosis.medical_code + "</b><br>"

    if mtuha_missing:
        frappe.throw(
            _("{0}<br>MTUHA Code not defined for the above diagnosis").format(mtuha_missing))

    insurance_subscription = doc.insurance_subscription
    child_tables = {
        "drug_prescription": "drug_code",
        "lab_test_prescription": "lab_test_code",
        "procedure_prescription": "procedure",
        "radiology_procedure_prescription": "radiology_examination_template",
        "therapies": "therapy_type",
        # "diet_recommendation": "diet_plan" dosent have Healthcare Service Insurance Coverage
    }
    prescribed_list = ""
    for key, value in child_tables.items():
        table = doc.get(key)
        for row in table:
            quantity = row.get("quantity") or row.get("no_of_sessions")
            if (not doc.insurance_subscription) or row.prescribe or row.is_not_available_inhouse:
                row.prescribe = 1
                prescribed_list += "-  <b>" + row.get(value) + "</b><BR>"
            elif not row.prescribe:
                if row.get("no_of_sessions") and doc.insurance_subscription:
                    if row.no_of_sessions != 1:
                        row.no_of_sessions = 1
                        frappe.msgprint(_("No of sessions have been set to 1 for {0} as per insurance rules.").format(
                            row.get(value)), alert=True)
            if not row.is_not_available_inhouse:
                validate_stock_item(row.get(value), quantity, healthcare_service_unit=row.get(
                    "healthcare_service_unit"))
    if prescribed_list:
        frappe.msgprint(
            _("{0}<BR>The above been prescribed. <b>Request the patient to visit the cashier for cash payment</b> or prescription printout.").format(prescribed_list))

    if not insurance_subscription:
        return

    if not doc.healthcare_service_unit:
        # doc.healthcare_service_unit = "NW2F Room 05 OPD - SHMH-DSM"
        frappe.throw(_("Healthcare Service Unit not set"))
    healthcare_insurance_coverage_plan = frappe.get_value(
        "Healthcare Insurance Subscription", insurance_subscription, "healthcare_insurance_coverage_plan")
    if not healthcare_insurance_coverage_plan:
        frappe.throw(_("Healthcare Insurance Coverage Plan is Not defiend"))
    # hsic => Healthcare Service Insurance Coverage

    for key, value in child_tables.items():
        table = doc.get(key)
        for row in table:
            if row.override_subscription or row.prescribe:
                continue
            hsic_list = frappe.get_all("Healthcare Service Insurance Coverage",
                                       fields={"healthcare_service_template",
                                               "maximum_number_of_claims",
                                               "approval_mandatory_for_claim"},
                                       filters={
                                           "is_active": 1,
                                           "healthcare_insurance_coverage_plan": healthcare_insurance_coverage_plan,
                                           "start_date": ["<=", nowdate()],
                                           "end_date": [">=", nowdate()],
                                           "healthcare_service_template": row.get(value)
                                       },
                                       order_by="modified desc"
                                       )
            items_list = []
            if len(hsic_list) > 0:
                for i in hsic_list:
                    items_list.append(i.healthcare_service_template)
            if row.get(value) not in items_list:
                frappe.throw(_("{0} not covered in Healthcare Insurance Coverage Plan").format(
                    row.get(value)))
            else:
                maximum_number_of_claims = next(i for i in hsic_list if i["healthcare_service_template"] == row.get(
                    value)).get("maximum_number_of_claims")
                if maximum_number_of_claims == 0:
                    continue
                times = 12 / maximum_number_of_claims
                count = 1
                days = int(times * 30)
                if times < 0.1:
                    count = 1 / times
                    days = 30
                start = add_to_date(nowdate(), days=-days)
                end = nowdate()
                claims_count = frappe.get_all("Healthcare Insurance Claim", filters={
                    "service_template": row.get(value),
                    "insurance_subscription": insurance_subscription,
                    "claim_posting_date": ["between", start, end],
                })
                if len(claims_count) > count:
                    frappe.throw(_("Maximum Number of Claims for {0} per year is exceeded within the last {1} days").format(
                        row.get(value), days))
                row.is_restricted = next(i for i in hsic_list if i["healthcare_service_template"] == row.get(
                    value)).get("approval_mandatory_for_claim")
                if row.is_restricted:
                    frappe.msgprint(_(row.doctype + " row " + str(row.get(value)) +
                                      " requires additional authorization"), alert=True)
    validate_totals(doc)


def checkـforـduplicate(doc):
    items = []
    for item in doc.drug_prescription:
        if item.drug_code not in items:
            items.append(item.drug_code)
        else:
            frappe.throw(_("Drug '{0}' is duplicated in line '{1}' in Drug Prescription").format(
                item.drug_code, item.idx))


# def get_year_end(dt, as_str=False):
#     dt = getdate(dt)
#     DATE_FORMAT = "%Y-%m-%d"
#     date = datetime.date(dt.year, 12, 31)
#     return date.strftime(DATE_FORMAT) if as_str else date


@frappe.whitelist()
def duplicate_encounter(encounter):
    doc = frappe.get_doc("Patient Encounter", encounter)
    if not doc.docstatus == 1 or doc.encounter_type == 'Final' or doc.duplicated == 1:
        frappe.msgprint(
            _("This encounter cannot be duplicated. Check if it is already duplicated using Menu/Links."), alert=True)
        return
    encounter_doc = frappe.copy_doc(doc)
    encounter_dict = encounter_doc.as_dict()
    child_tables = {
        "drug_prescription": "previous_drug_prescription",
        "lab_test_prescription": "previous_lab_prescription",
        "procedure_prescription": "previous_procedure_prescription",
        "radiology_procedure_prescription": "previous_radiology_procedure_prescription",
        "therapies": "previous_therapy_plan_detail",
        "diet_recommendation": "previous_diet_recommendation"
    }

    fields_to_clear = ['name', 'owner', 'creation', 'modified', 'modified_by',
                       'docstatus', 'amended_from', 'amendment_date', 'parentfield', 'parenttype', 'sales_invoice']

    for key, value in child_tables.items():
        cur_table = encounter_dict.get(key)
        if not cur_table:
            continue
        for row in cur_table:
            new_row = row
            for fieldname in (fields_to_clear):
                new_row[fieldname] = None
            encounter_dict[value].append(new_row)
        encounter_dict[key] = []
    encounter_dict["duplicated"] = 0
    encounter_dict["encounter_type"] = "Ongoing"
    if not encounter_dict.get("reference_encounter"):
        encounter_dict["reference_encounter"] = doc.name
    encounter_dict["from_encounter"] = doc.name
    encounter_dict["previous_total"] = doc.previous_total + doc.current_total
    encounter_dict["current_total"] = 0
    encounter_doc = frappe.get_doc(encounter_dict)
    encounter_doc.save()
    frappe.msgprint(
        _('Patient Encounter {0} created'.format(encounter_doc.name)))
    doc.duplicated = 1
    doc.save()
    return encounter_doc.name


def get_item_info(item_code=None, medication_name=None):
    data = {}
    if not item_code and medication_name:
        item_code = frappe.get_value(
            "Medication", medication_name, "item")
    if item_code:
        is_stock, disabled = frappe.get_value(
            "Item", item_code, ["is_stock_item", "disabled"])
        data = {
            "item_code": item_code,
            "is_stock": is_stock,
            "disabled": disabled
        }
    return data


def get_stock_availability(item_code, warehouse):
    latest_sle = frappe.db.sql("""SELECT SUM(qty_after_transaction) AS actual_qty
        FROM `tabStock Ledger Entry` 
        WHERE item_code = %s AND warehouse = %s
        LIMIT 1""", (item_code, warehouse), as_dict=1)

    sle_qty = latest_sle[0].actual_qty or 0 if latest_sle else 0
    return sle_qty


@frappe.whitelist()
def validate_stock_item(healthcare_service, qty, warehouse=None, healthcare_service_unit=None, caller="Unknown"):
    # frappe.msgprint(_("{0} warehouse passed. <br> {1} healthcare service unit passed").format(warehouse, healthcare_service_unit), alert=True)
    if caller != "Drug Prescription" and not healthcare_service_unit:
        # LRPT code stock check goes here
        return

    if (frappe.get_value("Medication", healthcare_service, "is_not_available_inhouse")):
        return

    qty = float(qty)
    if qty == 0:
        qty = 1

    item_info = get_item_info(medication_name=healthcare_service)
    stock_qty = 0
    if healthcare_service_unit:
        warehouse = get_warehouse_from_service_unit(healthcare_service_unit)
        # frappe.msgprint(_("{0} selected using {1}").format(warehouse, healthcare_service_unit), alert=True)
    if not warehouse:
        frappe.throw(_("Warehouse is missing in healthcare service unit {0} when checking for {1}").format(
            healthcare_service_unit, item_info.get("item_code")))
    if item_info.get("is_stock") and item_info.get("item_code"):
        stock_qty = get_stock_availability(
            item_info.get("item_code"), warehouse) or 0
        if float(qty) > float(stock_qty):
            # To be removed after few months of stability. 2021-03-18 17:01:46
            # This is to avoid socketio diconnection when bench is restarted but user session is on.
            frappe.msgprint(_("The quantity required for the item {0} is INSUFFICIENT in {1}/{2}. Available quantity is {3}.").format(
                item_info.get("item_code"), warehouse, healthcare_service_unit, stock_qty), alert=True)
            frappe.throw(_("The quantity required for the item {0} is INSUFFICIENT in {1}/{2}. Available quantity is {3}.").format(
                item_info.get("item_code"), warehouse, healthcare_service_unit, stock_qty))
            return False
    if stock_qty > 0:
        frappe.msgprint(_("Available quantity for the item {0} in {1}/{2} is {3} pcs.").format(
            healthcare_service, warehouse, healthcare_service_unit, stock_qty), alert=True)
    return True


def on_submit(doc, method):
    create_healthcare_docs(doc)
    create_delivery_note(doc)
    update_inpatient_record_consultancy(doc)
    frappe.enqueue(method=enqueue_on_update_after_submit, queue='short',
                   timeout=10000, is_async=True, kwargs=doc.name)


@frappe.whitelist()
def create_healthcare_docs_from_name(patient_encounter_doc_name):
    patient_encounter_doc = frappe.get_doc(
        "Patient Encounter", patient_encounter_doc_name)
    create_healthcare_docs(patient_encounter_doc)
    create_delivery_note(patient_encounter_doc)


def create_healthcare_docs(patient_encounter_doc):
    if patient_encounter_doc.docstatus != 1:
        frappe.msgprint(
            _("Cannot process Patient Encounter that is not submitted! Please submit and try again."), alert=True)
        return
    if not patient_encounter_doc.appointment:
        frappe.msgprint(
            _("Patient Encounter does not have patient appointment number! Request for support with this message."), alert=True)
        return
    if not patient_encounter_doc.insurance_subscription:
        return

    child_tables_list = ["lab_test_prescription",
                         "radiology_procedure_prescription", "procedure_prescription"]
    for child_table_field in child_tables_list:
        if patient_encounter_doc.get(child_table_field):
            child_table = patient_encounter_doc.get(child_table_field)
            for child in child_table:
                if child.prescribe:
                    continue
                if child.doctype == "Lab Prescription":
                    create_individual_lab_test(patient_encounter_doc, child)
                elif child.doctype == "Radiology Procedure Prescription":
                    create_individual_radiology_examination(
                        patient_encounter_doc, child)
                elif child.doctype == "Procedure Prescription":
                    create_individual_procedure_prescription(
                        patient_encounter_doc, child)


def create_delivery_note(patient_encounter_doc):
    if not patient_encounter_doc.appointment:
        return
    insurance_subscription, insurance_company = frappe.get_value(
        "Patient Appointment", patient_encounter_doc.appointment, ["insurance_subscription", "insurance_company"])
    if not insurance_subscription:
        return
    warehouses = []
    for line in patient_encounter_doc.drug_prescription:
        if line.prescribe:
            continue
        if line.drug_prescription_created:
            continue
        item_code = frappe.get_value("Medication", line.drug_code, "item_code")
        is_stock = frappe.get_value("Item", item_code, "is_stock_item")
        if not is_stock:
            continue
        warehouse = get_warehouse_from_service_unit(
            line.healthcare_service_unit)
        if warehouse and warehouse not in warehouses:
            warehouses.append(warehouse)
    for element in warehouses:
        items = []
        for row in patient_encounter_doc.drug_prescription:
            if row.prescribe:
                continue
            warehouse = get_warehouse_from_service_unit(
                row.healthcare_service_unit)
            if element != warehouse:
                continue
            item_code = frappe.get_value(
                "Medication", row.drug_code, "item_code")
            is_stock = frappe.get_value(
                "Item", item_code, "is_stock_item")
            item_name = frappe.get_value(
                "Item", item_code, "item_name")
            if not is_stock:
                continue
            item = frappe.new_doc("Delivery Note Item")
            item.item_code = item_code
            item.item_name = item_name
            item.warehouse = warehouse
            item.is_restricted = row.is_restricted
            item.qty = row.quantity or 1
            item.medical_code = row.medical_code
            item.rate = get_item_rate(
                item_code, patient_encounter_doc.company, insurance_subscription, insurance_company)
            item.reference_doctype = row.doctype
            item.reference_name = row.name
            item.description = row.drug_name + " for " + (row.dosage or "No Prescription Dosage") + " for " + \
                (row.period or "No Prescription Period") + " with " + row.medical_code + " and doctor notes: " + \
                (row.comment or "Take medication as per dosage.")
            items.append(item)
            row.drug_prescription_created = 1
            patient_encounter_doc.save()
            patient_encounter_doc.reload()
            frappe.db.commit()
        if len(items) == 0:
            continue
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
            reference_doctype=patient_encounter_doc.doctype,
            reference_name=patient_encounter_doc.name,
            patient=patient_encounter_doc.patient,
            patient_name=patient_encounter_doc.patient_name,
            healthcare_service_unit=patient_encounter_doc.healthcare_service_unit,
            healthcare_practitioner=patient_encounter_doc.practitioner
        ))
        doc.flags.ignore_permissions = True
        doc.set_missing_values()
        doc.insert(ignore_permissions=True)
        if doc.get('name'):
            frappe.msgprint(_('Pharmacy Dispensing/Delivery Note {0} created successfully.').format(
                frappe.bold(doc.name)))


@frappe.whitelist()
def get_chronic_diagnosis(patient):
    data = frappe.get_all("Codification Table",
                          filters={
                              "parent": patient,
                              "parenttype": "Patient",
                              "parentfield": "codification_table"
                          },
                          fields=["medical_code", "code", "description"]
                          )
    return data


@frappe.whitelist()
def get_chronic_medications(patient):
    data = frappe.get_all("Chronic Medications",
                          filters={
                              "parent": patient,
                              "parenttype": "Patient",
                              "parentfield": "chronic_medications"
                          },
                          fields=["*"]
                          )
    return data


def validate_totals(doc):
    if not doc.insurance_company or not doc.insurance_subscription or doc.insurance_company == "NHIF" or not doc.daily_limit or doc.daily_limit == 0:
        return
    if doc.encounter_type == "Initial":
        appointment_amount = frappe.get_value(
            "Patient Appointment", doc.appointment, "paid_amount")
        doc.previous_total = appointment_amount

    childs_map = [
        {
            "table": "lab_test_prescription",
            "doctype": "Lab Test Template",
            "item": "lab_test_code",
        },
        {
            "table": "radiology_procedure_prescription",
            "doctype": "Radiology Examination Template",
            "item": "radiology_examination_template",
        },
        {
            "table": "procedure_prescription",
            "doctype": "Clinical Procedure Template",
            "item": "procedure",
        },
        {
            "table": "drug_prescription",
            "doctype": "Medication",
            "item": "drug_code",
        },
        {
            "table": "therapies",
            "doctype": "Therapy Type",
            "item": "therapy_type",
        }
    ]
    doc.current_total = 0
    for child in childs_map:
        for row in doc.get(child.get("table")):
            if row.prescribe:
                continue
            item_code = frappe.get_value(
                child.get("doctype"), row.get(child.get("item")), "item")
            # Disabled items allowed to be used in Patient Encounter
            item_info = get_item_info(item_code=item_code)
            if item_info.get("disabled"):
                frappe.throw(_("The item {0} is disabled").format(item_code))
            item_rate = get_item_rate(
                item_code, doc.company, doc.insurance_subscription, doc.insurance_company)
            if hasattr(row, 'quantity'):
                quantity = row.quantity
            else:
                quantity = 1
            doc.current_total += item_rate * quantity
    diff = doc.daily_limit - doc.current_total - doc.previous_total
    if diff < 0:
        frappe.throw(_("The total daily limit of {0} for the Insurance Subscription {1} has been exceeded by {2}. <br> Please contact the reception to increase the limit or prescribe the items").format(
            doc.daily_limit, doc.insurance_subscription, diff))


@frappe.whitelist()
def finalized_encounter(cur_encounter, ref_encounter=None):
    frappe.set_value("Patient Encounter", cur_encounter,
                     "encounter_type", "Final")
    if not ref_encounter:
        frappe.set_value("Patient Encounter", cur_encounter,
                         "finalized", 1)
        return
    encounters_list = frappe.get_all("Patient Encounter", filters={
        "docstatus": 1,
        "reference_encounter": ref_encounter
    })
    for element in encounters_list:
        frappe.set_value("Patient Encounter", element.name, "finalized", 1)


@frappe.whitelist()
def create_sales_invoice(encounter, encounter_category, encounter_mode_of_payment):
    encounter_doc = frappe.get_doc(
        "Patient Encounter", encounter)
    encounter_category = frappe.get_doc(
        "Encounter Category", encounter_category)
    if not encounter_category.create_sales_invoice:
        return

    doc = frappe.new_doc('Sales Invoice')
    doc.patient = encounter_doc.patient
    doc.customer = frappe.get_value(
        'Patient', encounter_doc.patient, 'customer')
    doc.due_date = getdate()
    doc.company = encounter_doc.company
    doc.debit_to = get_receivable_account(
        encounter_doc.company)
    doc.healthcare_service_unit = encounter_doc.healthcare_service_unit
    doc.healthcare_practitioner = encounter_doc.practitioner

    item = doc.append('items', {})
    item.item_code = encounter_category.encounter_fee_item
    item.description = _('Consulting Charges: {0}').format(
        encounter_doc.practitioner)
    item.income_account = get_income_account(
        encounter_doc.practitioner, encounter_doc.company)
    item.cost_center = frappe.get_cached_value(
        'Company', encounter_doc.company, 'cost_center')
    item.qty = 1
    item.rate = encounter_category.encounter_fee
    item.reference_dt = 'Patient Encounter'
    item.reference_dn = encounter_doc.name
    item.amount = encounter_category.encounter_fee

    doc.is_pos = 1
    payment = doc.append('payments', {})
    payment.mode_of_payment = encounter_mode_of_payment
    payment.amount = encounter_category.encounter_fee

    doc.set_taxes()
    doc.flags.ignore_permissions = True
    doc.set_missing_values(for_validate=True)
    doc.flags.ignore_mandatory = True
    doc.save(ignore_permissions=True)
    doc.calculate_taxes_and_totals()
    doc.submit()
    frappe.msgprint(_('Sales Invoice {0} created'.format(
        doc.name)))
    encounter_doc.sales_invoice = doc.name
    encounter_doc.db_update()

    return "true"


def update_inpatient_record_consultancy(doc):
    if doc.inpatient_record:
        item_code = frappe.get_value("Healthcare Practitioner",
                                     doc.practitioner, "inpatient_visit_charge_item")
        rate = 0
        if doc.insurance_subscription:
            rate = get_item_rate(
                item_code, doc.company, doc.insurance_subscription, doc.insurance_company)
        elif doc.mode_of_payment:
            price_list = frappe.get_value(
                "Mode of Payment", doc.mode_of_payment, "price_list")
            rate = get_item_price(item_code, price_list, doc.company)

        record_doc = frappe.get_doc("Inpatient Record", doc.inpatient_record)
        row = record_doc.append("inpatient_consultancy", {})
        row.date = nowdate()
        row.consultation_item = item_code
        row.rate = rate
        row.encounter = doc.name
        record_doc.save(ignore_permissions=True)
        frappe.msgprint(
            _("Inpatient Consultancy recorde added for item {0}").format("item_code"), alert=True)


def on_update_after_submit(doc, method):
    if doc.is_not_billable:
        return
    child_tables_list = ["lab_test_prescription",
                         "radiology_procedure_prescription", "procedure_prescription", "drug_prescription"]
    services_created_pending = 0
    for child_table_field in child_tables_list:
        if services_created_pending:
            break
        if doc.get(child_table_field):
            child_table = doc.get(child_table_field)
            for child in child_table:
                if child.doctype == "Lab Prescription":
                    if child.lab_test_created == 0:
                        services_created_pending = 1
                        break
                elif child.doctype == "Radiology Procedure Prescription":
                    if child.radiology_examination_created == 0:
                        services_created_pending = 1
                        break
                elif child.doctype == "Procedure Prescription":
                    if child.procedure_created == 0:
                        services_created_pending = 1
                        break
                elif child.doctype == "Drug Prescription":
                    if child.drug_prescription_created == 0:
                        services_created_pending = 1
                        break
    if services_created_pending == 0:
        doc.db_set("is_not_billable", 1)
    else:
        doc.db_set("is_not_billable", 0)


def enqueue_on_update_after_submit(doc_name):
    time.sleep(5)
    on_update_after_submit(frappe.get_doc("Patient Encounter", doc_name))


def before_submit(doc, method):
    encounter_create_sales_invoice = frappe.get_value(
        "Encounter Category", doc.encounter_category, "create_sales_invoice")
    if encounter_create_sales_invoice:
        if not doc.sales_invoice:
            frappe.throw(_("The encounter cannot be submitted as the Sales Invoice is not created yet!<br><br>Click on Create Sales Invoice and Send to VFD before submitting.", "Cannot Submit Encounter"))
        vfd_status = frappe.get_value(
            "Sales Invoice", doc.sales_invoice, "vfd_status")
        if vfd_status == "Not Sent":
            frappe.throw(
                _("The encounter cannot be submitted as the Sales Invoice has not been sent to VFD!<br><br>Click on Send to VFD before submitting.", "Cannot Submit Encounter"))
