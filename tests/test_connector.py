from taoran_agent.connector import adapt_jiandaoyun_request, load_jiandaoyun_mapping
from taoran_agent.models import JiandaoyunCheckRequest


def test_json_subform_arrays_preserve_ids_stage_and_received_empty():
    request = JiandaoyunCheckRequest.model_validate({
        'context': {'tenant_id': 'tenant_demo', 'request_id': 'subform-json', 'user_id': 'TEST'},
        'form_data': {'participants': '[{"contact_id":"SYNTHETIC-C1"}]',
                      'opportunities': '[]'},
    })
    result = adapt_jiandaoyun_request(request, load_jiandaoyun_mapping())
    assert result.visit.participants[0].contact_id == 'SYNTHETIC-C1'
    assert result.visit.opportunities == []
    assert {'participants', 'opportunities'} <= set(result.visit.metadata['source_supplied_fields'])


def test_source_values_are_normalized() -> None:
    request = JiandaoyunCheckRequest.model_validate(
        {
            "context": {
                "tenant_id": "tenant_demo",
                "request_id": "jdy-001",
                "user_id": "EMP001",
                "source": "jiandaoyun",
            },
            "form_data": {
                "visit_date": "2026-08-18",
                "employee_id": "EMP001",
                "customer_type_ii": "商机客户",
                "visit_method": "面对面拜访",
                "is_appointment": "是",
                "self_assessment": "达到目的",
            },
        }
    )

    result = adapt_jiandaoyun_request(request, load_jiandaoyun_mapping())

    assert result.visit.customer_type_ii.value == "opportunity"
    assert result.visit.visit_method.value == "face_to_face"
    assert result.visit.is_appointment is True
    assert result.visit.self_assessment.value == "achieved"


def test_new_copy_field_names_and_subforms_are_adapted() -> None:
    mapping = load_jiandaoyun_mapping()
    request = JiandaoyunCheckRequest.model_validate(
        {
            "context": {
                "tenant_id": "tenant_demo",
                "request_id": "jdy-widget-001",
                "user_id": "EMP001",
                "source": "jiandaoyun",
            },
            "form_data": {
                "拜访日期": "2026-08-18",
                "销售代表（通讯录）": "EMP001",
                "客户分类II": "商机客户",
                "联系人信息": [{"关联数据-主键": "CONTACT001"}],
                "关联商机阶段信息": [
                    {
                        "商机编号": "OPP001",
                        "历史商机阶段": "P2",
                        "最新商机阶段": "P3",
                    }
                ],
            },
        }
    )

    result = adapt_jiandaoyun_request(request, mapping)

    assert result.visit.customer_type_ii.value == "opportunity"
    assert result.visit.participants[0].contact_id == "CONTACT001"
    assert result.visit.opportunities[0].opportunity_id == "OPP001"
    assert result.visit.opportunities[0].current_stage == "P3"
    assert result.visit.metadata["field_provenance"]["customer_type_ii"] == (
        "jiandaoyun_form_field"
    )
    assert result.visit.metadata["field_provenance"]["opportunities"] == (
        "jiandaoyun_subform"
    )
    assert result.visit.metadata["field_mapping_version"] == mapping["mapping_version"]


def test_actual_jiandaoyun_date_and_user_shapes_are_adapted() -> None:
    mapping = load_jiandaoyun_mapping()
    request = JiandaoyunCheckRequest.model_validate(
        {
            "context": {
                "tenant_id": "tenant_demo",
                "request_id": "jdy-actual-shape-001",
                "user_id": "POC_OPERATOR",
                "source": "jiandaoyun",
            },
            "form_data": {
                "_widget_1574480762008": "2026-08-18T16:00:00.000Z",
                "_widget_1574314917310": {
                    "name": "测试销售",
                    "username": "EMP001",
                    "departments": [1],
                },
            },
        }
    )

    result = adapt_jiandaoyun_request(request, mapping)

    assert result.visit.visit_date.isoformat() == "2026-08-19"
    assert result.visit.employee_id == "EMP001"
