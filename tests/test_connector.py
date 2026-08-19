from taoran_agent.connector import adapt_jiandaoyun_request, load_jiandaoyun_mapping
from taoran_agent.models import JiandaoyunCheckRequest


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
