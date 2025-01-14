frappe.ui.form.on('Healthcare Insurance Subscription', {
    setup: function (frm) {
        frm.set_query('healthcare_insurance_coverage_plan', function () {
			return {
				filters: { 'insurance_company': frm.doc.insurance_company }
			};
		});
    },
    coverage_plan_card_number: function(frm){
        frm.trigger("get_patient_name")
    },
    insurance_company: function(frm){
        frm.trigger("get_patient_name")
        frm.set_value("daily_limit", 0)
    },
    get_patient_name: function(frm){
        if (!frm.doc.coverage_plan_card_number || frm.doc.insurance_company != "NHIF") return
        frappe.show_alert({
            message:__("Getting patient's information from NHIF"),
            indicator:'green'
        }, 5);
            frappe.call({
            method: 'hms_tz.nhif.api.insurance_subscription.check_patient_info',
            args: {
                'card_no': frm.doc.coverage_plan_card_number,
                'patient': frm.doc.patient,
                'patient_name': frm.doc.patient_name
            },
            callback: function (data) {
                if (data.message && data.message != frm.doc.patient_name) {
                    frm.set_value("patient_name", data.message);
                }
            }
        });
    },
});