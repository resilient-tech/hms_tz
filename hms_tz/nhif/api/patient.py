# -*- coding: utf-8 -*-
# Copyright (c) 2020, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from hms_tz.nhif.api.token import get_nhifservice_token
from erpnext import get_default_company
import json
import requests
from time import sleep
from hms_tz.nhif.doctype.nhif_product.nhif_product import add_product
from hms_tz.nhif.doctype.nhif_scheme.nhif_scheme import add_scheme
from frappe.utils import getdate
from hms_tz.nhif.doctype.nhif_response_log.nhif_response_log import add_log
from hms_tz.nhif.api.healthcare_utils import remove_special_characters
from datetime import date


def validate(doc, method):
    # validate date of birth
    if date.today() < getdate(doc.dob):
        frappe.throw(_("The date of birth cannot be later than today's date"))
    # replace initial 0 with 255 and remove all the unnecessray characters
    doc.mobile = remove_special_characters(doc.mobile)
    if doc.mobile[0] == "0":
        doc.mobile = "255" + doc.mobile[1:]
    if doc.next_to_kin_mobile_no:
        doc.next_to_kin_mobile_no = remove_special_characters(
            doc.next_to_kin_mobile_no)
        if doc.next_to_kin_mobile_no[0] == "0":
            doc.next_to_kin_mobile_no = "255" + doc.next_to_kin_mobile_no[1:]
    validate_mobile_number(doc.name, doc.mobile)
    if not doc.is_new():
        update_patient_history(doc)


@frappe.whitelist()
def validate_mobile_number(doc_name, mobile=None):
    if mobile:
        mobile_patients_list = frappe.get_all("Patient",
                                              filters={
                                                  "mobile": mobile,
                                                  "name": ['!=', doc_name]
                                              }
                                              )
        if len(mobile_patients_list) > 0:
            frappe.msgprint(_("This mobile number is used by another patient"))


@frappe.whitelist()
def get_patient_info(card_no=None):
    if not card_no:
        frappe.msgprint(_("Please set Card No"))
        return
    # TODO: need to be fixed to support multiple company
    company = get_default_company()
    if not company:
        company = frappe.defaults.get_user_default("Company")
    if not company:
        company = frappe.get_list("Company NHIF Settings", fields=["company"], filters = {"enable": 1})[0].company
    if not company:
        frappe.throw(_("No companies found to connect to NHIF"))
    token = get_nhifservice_token(company)

    nhifservice_url = frappe.get_value(
        "Company NHIF Settings", company, "nhifservice_url")
    headers = {
        "Authorization": "Bearer " + token
    }
    url = str(nhifservice_url) + \
        "/nhifservice/breeze/verification/GetCardDetails?CardNo=" + \
        str(card_no)
    for i in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=5)
            r.raise_for_status()
            frappe.logger().debug({"webhook_success": r.text})
            if json.loads(r.text):
                add_log(
                    request_type="GetCardDetails",
                    request_url=url,
                    request_header=headers,
                    response_data=json.loads(r.text)
                )
                card = json.loads(r.text)
                frappe.msgprint(_(card["Remarks"]), alert=True)
                add_scheme(card.get("SchemeID"), card.get("SchemeName"))
                add_product(card.get("ProductCode"), card.get("ProductName"))
                return card
            else:
                add_log(
                    request_type="GetCardDetails",
                    request_url=url,
                    request_header=headers,
                )
                frappe.msgprint(json.loads(r.text))
                frappe.msgprint(_("Getting information from NHIF failed. Try again after sometime, or continue manually."))
        except Exception as e:
            frappe.logger().debug({"webhook_error": e, "try": i + 1})
            sleep(3 * i + 1)
            if i != 2:
                continue
            else:
                raise e


def update_patient_history(doc):
    # Remarked till multi company setting is required and feasible from Patient doctype 2021-03-20 19:57:14
    # company = get_default_company()
    # update_history = frappe.get_value(
    #     "Company NHIF Settings", company, "update_patient_history")
    # if not update_history:
    #     return

    medical_history = ""
    for row in doc.codification_table:
        if row.description:
            medical_history += row.description + "\n"
    doc.medical_history = medical_history

    medication = ""
    for row in doc.chronic_medications:
        if row.drug_name:
            medication += row.drug_name + "\n"
    doc.medication = medication
    if doc.medical_history or doc.medication:
        frappe.msgprint(_("Update patient history for medical history and chronic medications"), alert = True)


@frappe.whitelist()
def check_card_number(card_no, is_new=None, patient=None):
    filters = {
        "insurance_card_detail": ["like", "%" + card_no + "%"]
    }
    if not is_new and patient:
        filters["name"] = ['!=', patient]
    patients = frappe.get_all("Patient", filters=filters)
    if len(patients):
        return patients[0].name
    else:
        return "false"
