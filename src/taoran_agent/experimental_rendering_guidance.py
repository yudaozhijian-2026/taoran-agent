"""V36.1 rendering lookup. Consumes frozen state; never classifies raw input."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field


@dataclass(frozen=True)
class SemanticRenderingGuidance:
    guidance_code: str
    applicable_state: str
    business_meaning: str
    allowed_language: tuple[str, ...]
    forbidden_language: tuple[str, ...]
    suggestion_guidance: str
    priority: int = 100


def _g(code, state, meaning, allowed, forbidden, suggestion):
    return SemanticRenderingGuidance(code, state, meaning, tuple(allowed), tuple(forbidden), suggestion)


GUIDANCE = {g.guidance_code: g for g in (
    _g('FIELD_STATE_MISSING', 'missing', '表单缺失，不代表业务未发生', ['尚未填写'], ['已填写但不具体'], '补充该字段的实际信息'),
    _g('FIELD_STATE_PLACEHOLDER', 'placeholder', '字段已填占位值', ['已填写，但当前内容属于占位值，无法形成有效业务判断'], ['未填写', '未具体填写'], '明确有效期望结果，不代填业务目标'),
    _g('FIELD_STATE_VAGUE', 'vague', '已填目标缺少完成标准', ['已填写，但目标过于宽泛，当前缺少明确完成标准'], ['目标未达成', '记录不足以证明目标实现'], '先明确关键结果的验收标准'),
    _g('GOAL_NOT_ASSESSABLE', 'not_assessable', '无法准确判断达成程度', ['当前无法准确判断达成程度'], ['自评偏乐观', '应将自评修改为部分达成', '应将自评修改为未达成'], '建议先明确关键结果，不纠正自评'),
    _g('GOAL_SUPPORTED_WITH_MISSING_FEEDBACK', 'supported + feedback missing', '已有目标支持，反馈缺口独立', ['本次目标有事实支持；客户反馈尚未填写'], ['无客户反馈所以目标未完成'], '仅补充反馈记录，不追加完成条件'),
    _g('JOINT_AGREEMENT_RECORDED', 'JOINT_AGREEMENT.recorded', '双方已形成共同约定', ['已形成双方共同约定'], ['没有明确行动', '没有任何客户动作', '未形成约定'], '保留共同约定，只说明另外的信息缺口'),
    _g('JOINT_AGREEMENT_WITHOUT_COMMITMENT', 'joint recorded + commitment not_recorded', '共同约定不等于客户单方承诺', ['已形成双方共同约定；尚未记录客户另外需要完成的独立行动'], ['没有明确动作', '没有客户动作', '客户已单方承诺'], '共同约定与客户独立行动分开表述'),
    _g('NOT_RECORDED_CUSTOMER_ACTION', 'CUSTOMER_INDIVIDUAL_ACTION.not_recorded', '缺少记录不是否定事实', ['当前记录未体现客户的独立行动'], ['客户没有行动', '客户拒绝'], '仅询问尚未记录的信息'),
    _g('COMPLETED_EVENT_WITH_MISSING_FEEDBACK', 'actual action + feedback missing', '已发生动作不受反馈缺失影响', ['已完成的动作有事实支撑；客户反馈是另一项记录缺口'], ['缺少反馈所以动作未完成'], '保留动作主体与完成状态，反馈另述'),
    _g('COMPOSITE_GOAL_PARTIAL', 'partially_achieved', '各子目标分别对齐', ['已支持的目标成立；另一目标当前记录未体现'], ['两项目标均未达成'], '分别生成目标绑定，不跨目标推导'),
)}


def select_guidance(state):
    selected = []
    def add(code, target):
        selected.append({**asdict(GUIDANCE[code]), 'target': target})
    for field, value in state['field_states'].items():
        code = {'missing': 'FIELD_STATE_MISSING', 'placeholder': 'FIELD_STATE_PLACEHOLDER'}.get(value)
        if code:
            add(code, field)
    if state['relations']['JOINT_AGREEMENT']['status'] == 'recorded':
        add('JOINT_AGREEMENT_RECORDED', 'PROCESS')
    return selected


@dataclass
class RenderingContract:
    contract_id: str
    goal_id: str
    semantic_state: dict
    supporting_fact_ids: list
    relation_state: dict
    allowed_claim_types: list
    required_points: list
    forbidden_claims: list
    knowledge_guidance_codes: list
    suggestion_scope: list
    source_fields: list
    required: bool = True
    allowed_fact_ids: list = dataclass_field(default_factory=list)
    contradicting_fact_ids: list = dataclass_field(default_factory=list)


NEXT_ACTION_FIELDS = frozenset({'next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at'})


def next_action_fact_ids(state):
    """Inherit existing facts in section scope; never infer/create facts."""
    return [f['fact_id'] for f in state['facts']
            if (f['source_field'] in NEXT_ACTION_FIELDS or f['source_field'] in {'process_description','customer_feedback'} and f['fact_type'] == 'PLANNED_ACTION' and f['temporality'] == 'planned')
            and f['fact_type'] in {'PLANNED_ACTION', 'JOINT_AGREEMENT', 'CUSTOMER_COMMITMENT', 'CUSTOMER_ACTION', 'SYSTEM_FACT'}
            and f['actor'] in {'customer', 'sales', 'both', 'unknown', 'system'}
            and (f['temporality'] in {'actual', 'planned'} or f['fact_type'] == 'SYSTEM_FACT' and f['temporality'] == 'unknown')]


def next_step_proof_allowed(field, quote):
    """Process evidence is eligible only when its cited span includes a plan."""
    if field not in {'process_description', 'customer_feedback'}:
        return True
    from .experimental_business_semantic_state import build_business_state
    state = build_business_state({field: quote})
    return any(f['fact_type'] == 'PLANNED_ACTION' and f['temporality'] == 'planned'
               for f in state['facts'])


def rendering_input(state):
    guidance = select_guidance(state)
    contracts = []
    def add(key, goal_id, semantic, claims, facts, fields, required, targets, scope):
        relevant = [g for g in guidance if g['target'] in targets]
        contracts.append(asdict(RenderingContract(
            key, goal_id, deepcopy(semantic), list(facts), deepcopy(state['relations']), claims,
            [g['business_meaning'] for g in relevant],
            list(dict.fromkeys(x for g in relevant for x in g['forbidden_language'])),
            list(dict.fromkeys(g['guidance_code'] for g in relevant)), scope, fields, required)))
    for original in state['goal_items']:
        goal = {k: deepcopy(v) for k,v in original.items() if k not in {'status','assessability'}}
        goal['status'] = 'unassessed'
        add('C_' + goal['goal_id'], goal['goal_id'], {'goal': goal},
            ['supported','partially_supported','contradicted','unresolved','not_assessable'],
            [], [goal['source_field'], 'process_description', 'customer_feedback', 'self_assessment'],
            True, [goal['goal_id'], goal['source_field']], ['O_KR'])
    if not state['goal_items']:
        add('C_GOAL_FIELD', '', {'field': 'expected_key_result', 'state': state['field_states']['expected_key_result']},
            [state['field_states']['expected_key_result']], [], ['expected_key_result'], True, ['expected_key_result'], ['O_KR'])
    joint = state['relations']['JOINT_AGREEMENT']
    process_facts = [f['fact_id'] for f in state['facts'] if f['source_field'] in {'process_description', 'customer_feedback'}]
    add('C_PROCESS', '', {'facts': [f for f in state['facts'] if f['fact_id'] in process_facts]},
        ['joint_agreement_recorded'] if joint['status'] == 'recorded' else ['recorded_fact'],
        process_facts, ['process_description', 'customer_feedback'], joint['status'] == 'recorded', ['PROCESS'], ['R'])
    add('C_NEXT', '', {'field': 'next_action_expected_result', 'state': state['field_states']['next_action_expected_result']},
        ['planned'], [], ['next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at', 'process_description', 'customer_feedback'],
        state['field_states']['next_action_expected_result'] == 'placeholder',
        ['next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at'], ['N'])
    add('C_CONTEXT', '', {'state': 'context'}, ['context'], [], ['visit_date', 'customer_type_ii', 'visit_method', 'is_appointment', 'purpose_code', 'opportunity_stage', 'opportunity_stages'], False, [], [])
    for contract in contracts:
        if contract['contract_id'] == 'C_NEXT':
            contract['allowed_fact_ids'] = next_action_fact_ids(state)
        elif contract['goal_id']:
            contract['allowed_fact_ids'] = list(process_facts)
        else:
            contract['allowed_fact_ids'] = list(contract['supporting_fact_ids'])
    return {'RENDERING_CONTRACTS': contracts, 'KNOWLEDGE_GUIDANCE': guidance}


RENDERING_INSTRUCTION = """
契约只限定字段来源、原定目标和输出位置，不预先决定目标达成。按原文独立选择目标claim_type：supported、partially_supported、contradicted、unresolved或not_assessable。
目标点使用对应contract_id和goal_id，其他点省略goal_id。每项目标最多一点，不补造或交换原目标。达成或明确反证必须引用过程/反馈原文；目标本身和自评不能证明已发生。
每个required契约必须覆盖；C_PROCESS可分段，但不得占用目标所需名额。最多4点，不单独输出背景点，优先保留主要结果和限制。
proofs引用指定字段连续原文，不生成fact_ids、source_fields等机器元数据，不在用户文本里显示内部ID或枚举。
C_NEXT可引用过程/反馈中的明确未来计划，并保留计划状态；只有已写空的字段才可称未填写。
建议具体性和业务达成分别判断，不能要求先补完成结果再判断原目标具体性。
"""
