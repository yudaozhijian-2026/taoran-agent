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
        code = {'missing': 'FIELD_STATE_MISSING', 'placeholder': 'FIELD_STATE_PLACEHOLDER', 'vague': 'FIELD_STATE_VAGUE'}.get(value)
        if code:
            add(code, field)
    for goal in state['goal_items']:
        if goal['assessability'] != 'assessable':
            add('GOAL_NOT_ASSESSABLE', goal['goal_id'])
        if goal['status'] == 'supported' and state['field_states']['customer_feedback'] == 'missing':
            add('GOAL_SUPPORTED_WITH_MISSING_FEEDBACK', goal['goal_id'])
    rel = state['relations']
    if rel['JOINT_AGREEMENT']['status'] == 'recorded':
        add('JOINT_AGREEMENT_RECORDED', 'PROCESS')
        if rel['CUSTOMER_COMMITMENT']['status'] == 'not_recorded':
            add('JOINT_AGREEMENT_WITHOUT_COMMITMENT', 'PROCESS')
    if rel['CUSTOMER_INDIVIDUAL_ACTION']['status'] == 'not_recorded':
        add('NOT_RECORDED_CUSTOMER_ACTION', 'PROCESS')
    if state['field_states']['customer_feedback'] == 'missing' and any(f['temporality'] == 'actual' and f['fact_type'] in {'COMPLETED_EVENT', 'SALES_ACTION', 'CUSTOMER_ACTION'} for f in state['facts']):
        add('COMPLETED_EVENT_WITH_MISSING_FEEDBACK', 'PROCESS')
    if state['self_assessment_alignment']['computed_goal_summary'] == 'partially_achieved':
        add('COMPOSITE_GOAL_PARTIAL', 'GOALS')
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
            if f['source_field'] in NEXT_ACTION_FIELDS
            and f['fact_type'] in {'PLANNED_ACTION', 'JOINT_AGREEMENT', 'CUSTOMER_COMMITMENT', 'CUSTOMER_ACTION', 'SYSTEM_FACT'}
            and f['actor'] in {'customer', 'sales', 'both', 'unknown', 'system'}
            and (f['temporality'] in {'actual', 'planned'} or f['fact_type'] == 'SYSTEM_FACT' and f['temporality'] == 'unknown')]


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
    for goal in state['goal_items']:
        alignment = next(a for a in state['goal_fact_alignments'] if a['goal_id'] == goal['goal_id'])
        claim = 'not_assessable' if goal['assessability'] != 'assessable' else goal['status']
        add('C_' + goal['goal_id'], goal['goal_id'], {'goal': goal, 'alignment': alignment}, [claim],
            alignment['supporting_fact_ids'], [goal['source_field'], 'process_description', 'customer_feedback', 'self_assessment'],
            True, [goal['goal_id'], goal['source_field'], 'GOALS'], ['O_KR'])
    if not state['goal_items']:
        add('C_GOAL_FIELD', '', {'field': 'expected_key_result', 'state': state['field_states']['expected_key_result']},
            [state['field_states']['expected_key_result']], [], ['expected_key_result'], True, ['expected_key_result'], ['O_KR'])
    joint = state['relations']['JOINT_AGREEMENT']
    process_facts = [f['fact_id'] for f in state['facts'] if f['source_field'] in {'process_description', 'customer_feedback'}]
    add('C_PROCESS', '', {'facts': [f for f in state['facts'] if f['fact_id'] in process_facts]},
        ['joint_agreement_recorded'] if joint['status'] == 'recorded' else ['recorded_fact'],
        process_facts, ['process_description', 'customer_feedback'], joint['status'] == 'recorded', ['PROCESS'], ['R'])
    add('C_NEXT', '', {'field': 'next_action_expected_result', 'state': state['field_states']['next_action_expected_result']},
        ['planned'], [], ['next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at'],
        state['field_states']['next_action_expected_result'] == 'placeholder',
        ['next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at'], ['N'])
    add('C_CONTEXT', '', {'state': 'context'}, ['context'], [], ['visit_date', 'customer_type_ii', 'visit_method', 'is_appointment', 'purpose_code', 'opportunity_stage', 'opportunity_stages'], False, [], [])
    for contract in contracts:
        if contract['contract_id'] == 'C_NEXT':
            contract['allowed_fact_ids'] = next_action_fact_ids(state)
        elif contract['goal_id']:
            alignment = next(a for a in state['goal_fact_alignments'] if a['goal_id'] == contract['goal_id'])
            contract['contradicting_fact_ids'] = list(alignment['contradicting_fact_ids'])
            contract['allowed_fact_ids'] = list(dict.fromkeys(alignment['supporting_fact_ids'] + alignment['contradicting_fact_ids']))
        else:
            contract['allowed_fact_ids'] = list(contract['supporting_fact_ids'])
    return {'RENDERING_CONTRACTS': contracts, 'KNOWLEDGE_GUIDANCE': guidance}


RENDERING_INSTRUCTION = '''
V36.4b渲染优先级：Semantic State > Rendering Contract > Knowledge Guidance > Raw Text。
不重新判断状态。原始文本仅用于连续原文引用和自然表达。其他旧提示如与状态冲突，以本契约为准。
analysis_points的目标点只需contract_id、goal_id、claim_type、text，非目标点省略goal_id；另保留既有kind/proofs原文证据协议。
不要生成fact_ids、source_fields或其他机器元数据，程序会由状态和契约自动补齐。
目标和下一步contract_id各最多一项；C_PROCESS允许承载多条不同过程事实，每条保留自己的文字、主体、状态和连续原文证据，程序将它们归入同一过程组，不强行合并句子。
先覆盖全部required=true的契约，C_PROCESS至少一项且可分段。goal_id与claim_type必须精确选自该契约，非目标项不需要goal_id。
每个目标独立一句，不得只写正确标签却表达另一目标。proofs仍用原字段原文；不要引用ID或状态作为证据，不需要把全部状态事实重复引一遍。
最多4项，按RENDERING_OUTPUT_PLAN先输出全部必需契约，再补过程与下一步；不单独输出visit_context，过程分段不能占用目标所需名额。过程事实可用customer_fact；G1/G2用objective_result。
每项尽量40字，最多55字。目标引用勿写成既成承诺；unresolved优先“当前记录尚未体现……”开头。不得接着写“客户未就此作出承诺”；只有原文明确否定或拒绝，才能表述对应否定事实。
vague/goal_not_assessable表示目标本身缺少标准：说已填但缺少明确验收标准、无法准确判断达成程度，不纠正自评。
self_assessment_not_assessable而目标assessable时，可以说当前记录对自评的支撑不足，建议补充相关事实后再校准自评；不得直接说自评错误、偏乐观或应改成部分达成/未达成。不要在用户文字里说“不纠正自评”等内部操作口令。
placeholder必须说已填写但属于占位内容，不能说未具体填写；联系时间缺失另句陈述。
共同约定必须承认双方已形成约定；R建议如有缺口只说明客户另外需要完成的独立行动尚未记录。
禁止以客户反馈缺失否认已完成销售代收。建议与分析同样服从状态和Guidance。
只输出完整JSON，内部绑定ID不得出现在text或suggestion中，用户只看到自然文本。
'''
