var patient_age = null

frappe.ui.form.on('Lab Test', {
    setup: function (frm) {
        frm.get_field('normal_test_items').grid.editable_fields = [
            { fieldname: 'lab_test_name', columns: 2 },
            { fieldname: 'lab_test_event', columns: 2 },
            { fieldname: 'result_value', columns: 2 },
            { fieldname: 'lab_test_uom', columns: 1 },
            { fieldname: 'detailed_normal_range', columns: 2 },
            { fieldname: 'result_status', columns: 1 }
        ]
        get_patient_age(frm)
		frm.get_field('descriptive_test_items').grid.editable_fields = [
			{ fieldname: 'lab_test_particulars', columns: 3 },
            { fieldname: 'result_component_option', columns: 4 },
			{ fieldname: 'result_value', columns: 4 }
		];
		frm.set_query("result_component_option", "descriptive_test_items", function(doc, cdt, cdn) {
			let d = locals[cdt][cdn];
			return {
				filters: [
					['Result Component Option', 'result_component', '=', d.lab_test_particulars]
				]
			};
		});
    },
    patient: function (frm) {
        get_patient_age(frm)
    },

});


frappe.ui.form.on('Normal Test Result', {
    result_value: function (frm, cdn, cdt) {
        if (!frm.doc.lab_test_name) {
            return
        }
        const row = locals[cdn][cdt];
        frappe.call({
            'method': 'hms_tz.nhif.api.lab_test.get_normals',
            args: {
                lab_test_name: row.lab_test_name,
                patient_age: patient_age,
                patient_sex: frm.doc.patient_sex
            },
            callback: function (data) {
                if (data.message) {
                    const r = data.message
                    row.min_normal = r.min
                    row.max_normal = r.max
                    row.text_normal = r.text
                    const data_normals = calc_data_normals(r, row.result_value)
                    row.detailed_normal_range = data_normals.detailed_normal_range;
                    row.result_status = data_normals.result_status;
                    frm.refresh_field("normal_test_items");
                }
            }
        });

    }
});

var calc_data_normals = function (data, value) {
    const result = {
        detailed_normal_range : "",
        result_status : ""
    };
    if (data.min && !data.max) {
        result.detailed_normal_range = "> " + data.min
        if (value > data.min) {
            result.result_status = "N"
        }
        else {
            result.result_status = "L"
        }
    }
    else if (!data.min && data.max) {
        result.detailed_normal_range = "< " + data.max
        if (value < data.max) {
            result.result_status = "N"
        }
        else {
            result.result_status = "H"
        }
    }
    else if (data.min && data.max) {
        result.detailed_normal_range = data.min + " - " + data.max
        if (value > data.min && value < data.max) {
            result.result_status = "N"
        }
        else if (value < data.min) {
            result.result_status = "L"
        }
        else if (value > data.max) {
            result.result_status = "H"
        }
    }
    if (data.text) {
        if (result.detailed_normal_range) {
            result.detailed_normal_range += " / "
        }
        result.detailed_normal_range += data.text
    }
    return result
}

var get_patient_age = function (frm) {
    if (frm.doc.patient) {
        frappe.call({
            'method': 'hms_tz.hms_tz.doctype.patient.patient.get_patient_detail',
            args: { patient: frm.doc.patient },
            async: false,
            callback: function (data) {
                if (data.message.dob) {
                    patient_age = calculate_age(data.message.dob);
                }
            }
        });
        return patient_age
    }
};


var calculate_age = function (dob) {
    var ageMS = Date.parse(Date()) - Date.parse(dob);
    var age = new Date();
    age.setTime(ageMS);
    var years = age.getFullYear() - 1970;
    return years
};

