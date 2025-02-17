# -*- coding: utf-8 -*-
# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe, json
from frappe import _
from frappe.utils import today, now_datetime, getdate, get_datetime
from frappe.model.document import Document
from frappe.desk.reportview import get_match_cond

class InpatientRecord(Document):
	def after_insert(self):
		frappe.db.set_value('Patient', self.patient, 'inpatient_record', self.name)
		frappe.db.set_value('Patient', self.patient, 'inpatient_status', self.status)

		if self.admission_encounter: # Update encounter
			frappe.db.set_value('Patient Encounter', self.admission_encounter, 'inpatient_record', self.name)
			frappe.db.set_value('Patient Encounter', self.admission_encounter, 'inpatient_status', self.status)

	def validate(self):
		self.validate_dates()
		self.validate_already_scheduled_or_admitted()
		if self.status == "Discharged":
			frappe.db.set_value("Patient", self.patient, "inpatient_status", None)
			frappe.db.set_value("Patient", self.patient, "inpatient_record", None)

	def validate_dates(self):
		if (getdate(self.expected_discharge) < getdate(self.scheduled_date)) or \
			(getdate(self.discharge_ordered_date) < getdate(self.scheduled_date)):
			frappe.throw(_('Expected and Discharge dates cannot be less than Admission Schedule date'))

		for entry in self.inpatient_occupancies:
			if entry.check_in and entry.check_out and \
				get_datetime(entry.check_in) > get_datetime(entry.check_out):
				frappe.throw(_('Row #{0}: Check Out datetime cannot be less than Check In datetime').format(entry.idx))

	def validate_already_scheduled_or_admitted(self):
		query = """
			select name, status
			from `tabInpatient Record`
			where (status = 'Admitted' or status = 'Admission Scheduled')
			and name != %(name)s and patient = %(patient)s
			"""

		ip_record = frappe.db.sql(query,{
				"name": self.name,
				"patient": self.patient
			}, as_dict = 1)

		if ip_record:
			msg = _(("Already {0} Patient {1} with Inpatient Record ").format(ip_record[0].status, self.patient) \
				+ """ <b><a href="#Form/Inpatient Record/{0}">{0}</a></b>""".format(ip_record[0].name))
			frappe.throw(msg)

	def admit(self, service_unit, check_in, expected_discharge=None):
		admit_patient(self, service_unit, check_in, expected_discharge)

	def discharge(self):
		discharge_patient(self)

	def transfer(self, service_unit, check_in, leave_from):
		if leave_from:
			patient_leave_service_unit(self, check_in, leave_from)
		if service_unit:
			transfer_patient(self, service_unit, check_in)


@frappe.whitelist()
def schedule_inpatient(args):
	admission_order = json.loads(args) # admission order via Encounter
	if not admission_order or not admission_order['patient'] or not admission_order['admission_encounter']:
		frappe.throw(_('Missing required details, did not create Inpatient Record'))

	inpatient_record = frappe.new_doc('Inpatient Record')

	# Admission order details
	set_details_from_ip_order(inpatient_record, admission_order)

	# Patient details
	patient = frappe.get_doc('Patient', admission_order['patient'])
	inpatient_record.patient = patient.name
	inpatient_record.patient_name = patient.patient_name
	inpatient_record.gender = patient.sex
	inpatient_record.blood_group = patient.blood_group
	inpatient_record.dob = patient.dob
	inpatient_record.mobile = patient.mobile
	inpatient_record.email = patient.email
	inpatient_record.phone = patient.phone
	inpatient_record.scheduled_date = today()

	# Set encounter detials
	encounter = frappe.get_doc('Patient Encounter', admission_order['admission_encounter'])
	if encounter and encounter.symptoms: # Symptoms
		set_ip_child_records(inpatient_record, 'chief_complaint', encounter.symptoms)

	if encounter and encounter.diagnosis: # Diagnosis
		set_ip_child_records(inpatient_record, 'diagnosis', encounter.diagnosis)

	if encounter and encounter.drug_prescription: # Medication
		set_ip_child_records(inpatient_record, 'drug_prescription', encounter.drug_prescription)

	if encounter and encounter.lab_test_prescription: # Lab Tests
		set_ip_child_records(inpatient_record, 'lab_test_prescription', encounter.lab_test_prescription)

	if encounter and encounter.procedure_prescription: # Procedure Prescription
		set_ip_child_records(inpatient_record, 'procedure_prescription', encounter.procedure_prescription)

	if encounter and encounter.therapies: # Therapies
		inpatient_record.therapy_plan = encounter.therapy_plan
		set_ip_child_records(inpatient_record, 'therapies', encounter.therapies)

	if encounter and encounter.radiology_procedure_prescription: # radiology
		set_ip_child_records(inpatient_record, 'radiology_procedure_prescription', encounter.radiology_procedure_prescription)

	if encounter and encounter.diet_recommendation: # diet Prescription
		set_ip_child_records(inpatient_record, 'diet_recommendation', encounter.diet_recommendation)

	if encounter and encounter.source: # Source
		inpatient_record.source = encounter.source

	if encounter and encounter.referring_practitioner: #  Referring Practitioner
		inpatient_record.referring_practitioner = encounter.referring_practitioner

	inpatient_record.status = 'Admission Scheduled'
	inpatient_record.save(ignore_permissions = True)

@frappe.whitelist()
def schedule_discharge(args):
	discharge_order = json.loads(args)
	inpatient_record_id = frappe.db.get_value('Patient', discharge_order['patient'], 'inpatient_record')
	if inpatient_record_id:
		inpatient_record = frappe.get_doc('Inpatient Record', inpatient_record_id)
		check_out_inpatient(inpatient_record)
		set_details_from_ip_order(inpatient_record, discharge_order)
		inpatient_record.status = 'Discharge Scheduled'
		inpatient_record.save(ignore_permissions = True)
		frappe.db.set_value('Patient', discharge_order['patient'], 'inpatient_status', inpatient_record.status)
		frappe.db.set_value('Patient Encounter', inpatient_record.discharge_encounter, 'inpatient_status', inpatient_record.status)

def set_details_from_ip_order(inpatient_record, ip_order):
	for key in ip_order:
		inpatient_record.set(key, ip_order[key])

def set_ip_child_records(inpatient_record, inpatient_record_child, encounter_child):
	for item in encounter_child:
		table = inpatient_record.append(inpatient_record_child)
		for df in table.meta.get('fields'):
			table.set(df.fieldname, item.get(df.fieldname))

def check_out_inpatient(inpatient_record):
	if inpatient_record.inpatient_occupancies:
		for inpatient_occupancy in inpatient_record.inpatient_occupancies:
			if inpatient_occupancy.left != 1:
				inpatient_occupancy.left = True
				inpatient_occupancy.check_out = now_datetime()
				frappe.db.set_value("Healthcare Service Unit", inpatient_occupancy.service_unit, "occupancy_status", "Vacant")

def discharge_patient(inpatient_record):
	validate_invoiced_inpatient(inpatient_record)
	inpatient_record.discharge_date = today()
	inpatient_record.status = "Discharged"

	inpatient_record.save(ignore_permissions = True)

def validate_invoiced_inpatient(inpatient_record):
	pending_invoices = []
	if inpatient_record.inpatient_occupancies:
		service_unit_names = False
		for inpatient_occupancy in inpatient_record.inpatient_occupancies:
			if inpatient_occupancy.invoiced != 1:
				if service_unit_names:
					service_unit_names += ", " + inpatient_occupancy.service_unit
				else:
					service_unit_names = inpatient_occupancy.service_unit
		if service_unit_names:
			pending_invoices.append("Inpatient Occupancy (" + service_unit_names + ")")

	docs = ["Patient Appointment", "Patient Encounter", "Lab Test", "Clinical Procedure"]

	for doc in docs:
		doc_name_list = get_inpatient_docs_not_invoiced(doc, inpatient_record)
		if doc_name_list:
			pending_invoices = get_pending_doc(doc, doc_name_list, pending_invoices)

	if pending_invoices:
		frappe.throw(_("Can not mark Inpatient Record Discharged, there are Unbilled Invoices {0}").format(", "
			.join(pending_invoices)), title=_('Unbilled Invoices'))

def get_pending_doc(doc, doc_name_list, pending_invoices):
	if doc_name_list:
		doc_ids = False
		for doc_name in doc_name_list:
			if doc_ids:
				doc_ids += ", "+doc_name.name
			else:
				doc_ids = doc_name.name
		if doc_ids:
			pending_invoices.append(doc + " (" + doc_ids + ")")

	return pending_invoices

def get_inpatient_docs_not_invoiced(doc, inpatient_record):
	return frappe.db.get_list(doc, filters = {'patient': inpatient_record.patient,
					'inpatient_record': inpatient_record.name, 'docstatus': 1, 'invoiced': 0})

def admit_patient(inpatient_record, service_unit, check_in, expected_discharge=None):
	inpatient_record.admitted_datetime = check_in
	inpatient_record.status = 'Admitted'
	inpatient_record.expected_discharge = expected_discharge

	inpatient_record.set('inpatient_occupancies', [])
	transfer_patient(inpatient_record, service_unit, check_in)

	frappe.db.set_value('Patient', inpatient_record.patient, 'inpatient_status', 'Admitted')
	frappe.db.set_value('Patient', inpatient_record.patient, 'inpatient_record', inpatient_record.name)

def transfer_patient(inpatient_record, service_unit, check_in):
	item_line = inpatient_record.append('inpatient_occupancies', {})
	item_line.service_unit = service_unit
	item_line.check_in = check_in

	inpatient_record.save(ignore_permissions = True)

	frappe.db.set_value("Healthcare Service Unit", service_unit, "occupancy_status", "Occupied")

def patient_leave_service_unit(inpatient_record, check_out, leave_from):
	if inpatient_record.inpatient_occupancies:
		for inpatient_occupancy in inpatient_record.inpatient_occupancies:
			if inpatient_occupancy.left != 1 and inpatient_occupancy.service_unit == leave_from:
				inpatient_occupancy.left = True
				inpatient_occupancy.check_out = check_out
				frappe.db.set_value("Healthcare Service Unit", inpatient_occupancy.service_unit, "occupancy_status", "Vacant")
	inpatient_record.save(ignore_permissions = True)

@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_leave_from(doctype, txt, searchfield, start, page_len, filters):
	docname = filters['docname']

	query = '''select io.service_unit
		from `tabInpatient Occupancy` io, `tabInpatient Record` ir
		where io.parent = '{docname}' and io.parentfield = 'inpatient_occupancies'
		and io.left!=1 and io.parent = ir.name'''

	return frappe.db.sql(query.format(**{
		"docname":	docname,
		"searchfield":	searchfield,
		"mcond":	get_match_cond(doctype)
	}), {
		'txt': "%%%s%%" % txt,
		'_txt': txt.replace("%", ""),
		'start': start,
		'page_len': page_len
	})
