"""V36 deterministic, conservative business state. No score or model call.

Only explicit constructions are resolved. Unrecognised semantics stay unknown;
source spans, not normalized descriptions, remain the evidence boundary.
"""
import re
from dataclasses import asdict, dataclass

VERSION = 'business-semantic-state-v36'
FIELD_NAMES = (
    'expected_key_result', 'purpose_code', 'other_purpose', 'process_description',
    'customer_feedback', 'self_assessment', 'deviation_reason', 'next_action_purpose',
    'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at',
    'visit_date', 'customer_type_ii', 'visit_method', 'is_appointment',
    'opportunity_stage', 'opportunity_stages',
)
GOAL_FIELDS = {'expected_key_result', 'next_action_expected_result'}
PLAN_FIELDS = {'next_action_purpose', 'next_action_other_purpose', 'next_action_expected_result', 'next_contact_at'}
_PLACEHOLDER = re.compile(r'(?:\d+|[\s.\-_/]+|测试|无|待定|待填|暂无|不详)\Z')
_OBSERVABLE = re.compile(r'确认|同意|承诺|签字|签署|下单|收货|代收|获得参与权|引荐|(?:沟通|了解).*(?:采购|预算|审批)')
_ACTION_START = re.compile(r'^(?:客户|双方|我方|销售)?(?:确认|同意|承诺|完成|签署|签字|下单|提交|转告|反馈|参加|获得|收集|了解|沟通|收货|代收)')
_CUSTOMER = re.compile(r'^(?:客户|负责人|[\u4e00-\u9fff]{1,6}(?:部长|经理|主管|主任))')
_SALES = re.compile(r'^(?:销售|我方|我司|业务员|我|(?:向|给)客户介绍|现场给客户收货)')
_NEGATIVE = re.compile(r'没有|暂无|暂不|不再|不会|拒绝|未能|尚无|并无|尚未|不同意|不承诺|不愿|不接受|未(?:确认|同意|完成|签署|签字|下单)')
_FUTURE = re.compile(r'计划|打算|下周|下次|明天|后续|(?:会|将|先将).*(?:转|发|反馈|签|下单)|待.*(?:签字|审批)')
_COMMIT = re.compile(r'同意|承诺|答应|(?:表示|说|称).*(?:会|先将|将)')
_TRANSFER = re.compile(r'转告|转交|转发|(?:资料|情况|信息).*(?:反馈|发给)|(?:反馈|发给).*(?:QA|采购|生产|部门)')
_TOPICS = {
    'needs': r'需求|采购计划|增[加供].*(?:二供|供应商)|增加二供',
    'conditions': r'具体条件|反馈条件',
    'contract': r'合同|最终版本',
    'budget': r'预算', 'procurement': r'采购|物资|审批|对账|对帐',
    'receipt': r'代收|收货', 'referral': r'引荐',
    'participation': r'参与权|参加.*(?:评审|会议)|参与.*(?:评审|会议)',
    'review_time': r'(?:评审|会议).*(?:时间|日期)',
    'sign': r'签署|签字', 'order': r'下单|点单',
}


@dataclass
class GoalItem:
    goal_id: str
    source_field: str
    source_text: str
    normalized_text: str
    assessability: str
    status: str = 'unassessed'
    operation: str = 'unknown'
    topics: tuple = ()


@dataclass
class RecordedFact:
    fact_id: str
    source_id: str
    source_field: str
    source_span: tuple
    text: str
    actor: str
    fact_type: str
    temporality: str
    polarity: str
    record_status: str
    topics: tuple = ()


@dataclass
class GoalFactAlignment:
    goal_id: str
    supporting_fact_ids: list
    contradicting_fact_ids: list
    unrelated_fact_ids: list
    alignment_status: str
    reason_code: str


def classify_field_state(field, value):
    """One presence/placeholder entry for all generator-visible fields.

    assessable for non-goal fields means usable field content, not goal success.
    False and zero are recorded values; digit strings are explicit placeholders.
    """
    if value is None or isinstance(value, str) and not value.strip() or isinstance(value, (list, dict, tuple)) and not value:
        return 'missing'
    if not isinstance(value, str):
        return 'assessable'
    text = value.strip()
    if _PLACEHOLDER.fullmatch(text):
        return 'placeholder'
    if field in GOAL_FIELDS | {'next_action_purpose', 'next_action_other_purpose', 'purpose_code', 'other_purpose'}:
        return 'assessable' if _OBSERVABLE.search(text) else 'vague'
    return 'assessable'


def _topics(text):
    return tuple(k for k, pattern in _TOPICS.items() if re.search(pattern, text)) + (('transfer',) if _TRANSFER.search(text) else ())


def _operation(text):
    if re.search(r'同意|承诺|答应|获得参与权', text):
        return 'commitment'
    if re.search(r'确认|沟通|了解', text):
        return 'information'
    if re.search(r'收货|代收|签字|签署|下单|提交', text):
        return 'execution'
    return 'unknown'


def decompose_goal(field, value):
    status = classify_field_state(field, value)
    if status in {'missing', 'placeholder'}:
        return []
    text = str(value).strip()
    parts, cursor = [], 0
    for separator in re.finditer(r'[，,]?\s*(?:并且|以及|同时|并|且|、|[；;])\s*', text):
        tail = text[separator.end():]
        if _ACTION_START.search(tail.strip()):
            parts.append(text[cursor:separator.start()].strip())
            cursor = separator.end()
    parts.append(text[cursor:].strip())
    return [GoalItem(f'G{i + 1}', field, part, re.sub(r'\s+', '', part),
        classify_field_state(field, part), operation=_operation(part), topics=_topics(part))
        for i, part in enumerate(parts) if part.strip()]


def _facts(context):
    result = []
    def add(field, match, actor, kind, time, polarity):
        text = match.group()
        result.append(RecordedFact(f'F{len(result) + 1}', f'{field}:{match.start()}:{match.end()}',
            field, (match.start(), match.end()), text, actor, kind, time, polarity,
            'unknown' if time == 'unknown' else 'negative_fact' if polarity == 'negative' else 'recorded', _topics(text)))
    for field in ('process_description', 'customer_feedback', 'self_assessment', *sorted(PLAN_FIELDS)):
        source = context.get(field)
        if classify_field_state(field, source) in {'missing', 'placeholder'} or not isinstance(source, str):
            continue
        actor = 'unknown'
        last_end = 0
        for match in re.finditer(r'[^，,。！？；;\n]+', source):
            text = match.group().strip()
            if re.search(r'[。！？；;\n]', source[last_end:match.start()]):
                actor = 'unknown'
            last_end = match.end()
            if field == 'self_assessment':
                add(field, match, 'sales', 'SYSTEM_FACT', 'self_assessed', 'unknown')
                continue
            if field in PLAN_FIELDS:
                add(field, match, 'unknown', 'PLANNED_ACTION', 'planned', 'unknown')
                continue
            # No ownership/occurrence inference from hypothetical or explicitly
            # absent recordings. Such text remains available as unknown context.
            if re.search(r'^(?:如果|假如|假设)|(?:未记录|未体现|未提及).*(?:客户|约定)|尚不清楚|不确定|待确认', text):
                add(field, match, 'unknown', 'SYSTEM_FACT', 'unknown', 'unknown')
                continue
            if _SALES.search(text):
                actor = 'sales'
            elif _CUSTOMER.search(text):
                actor = 'customer'
            if re.search(r'(?:双方|与客户|和客户).*(?:约定|商定)|^约定(?:明天|下次|再次)|^并?约定', text) and not re.search(r'未约定|没有约定|计划约定|希望约定', text):
                add(field, match, 'both', 'JOINT_AGREEMENT', 'actual', 'positive')
                continue
            negative = bool(_NEGATIVE.search(text))
            polarity = 'negative' if negative else 'positive'
            future = bool(_FUTURE.search(text))
            # An explicit negative plan is an actual statement about current
            # plans, not the completion of a planned action.
            if actor == 'customer' and negative:
                add(field, match, actor, 'CUSTOMER_REJECTION' if '拒绝' in text else 'CUSTOMER_STATEMENT', 'actual', 'negative')
                continue
            if actor == 'customer' and _COMMIT.search(text):
                add(field, match, actor, 'CUSTOMER_COMMITMENT', 'actual', polarity)
                if future or _TRANSFER.search(text):
                    add(field, match, actor, 'PLANNED_ACTION', 'planned', polarity)
                continue
            if future and not re.search(r'已经|已完成|已提交|已将|已经把|已把', text):
                add(field, match, actor, 'PLANNED_ACTION', 'planned', polarity)
                continue
            kind = 'SALES_ACTION' if actor == 'sales' else 'CUSTOMER_STATEMENT' if actor == 'customer' and re.search(r'反馈|表示|说|称', text) else 'CUSTOMER_ACTION' if actor == 'customer' else 'COMPLETED_EVENT' if re.search(r'已完成|已提交|已下单|已签', text) else 'SYSTEM_FACT'
            time = 'actual' if kind != 'SYSTEM_FACT' else 'unknown'
            add(field, match, actor, kind, time, polarity)
    return result


def _align(goal, facts):
    supporting, contradicting, unrelated = [], [], []
    if goal.assessability != 'assessable':
        return GoalFactAlignment(goal.goal_id, [], [], [f.fact_id for f in facts], 'not_assessable', 'GOAL_NOT_ASSESSABLE')
    for fact in facts:
        related = bool(set(goal.topics) & set(fact.topics))
        # Feedback of conditions is not just any transfer or feedback.
        if 'conditions' in goal.topics:
            related = 'conditions' in fact.topics
        # Keep goal-specific product/department tokens if provided.
        identifiers = re.findall(r'[A-Za-z][A-Za-z0-9]*', goal.source_text)
        if any(token.lower() not in fact.text.lower() for token in identifiers):
            related = False
        eligible = fact.temporality == 'actual' and fact.source_field in {'process_description', 'customer_feedback'}
        if goal.source_text.startswith('客户') and fact.actor not in {'customer', 'both'}:
            eligible = False
        supported = False
        contradiction = False
        if related and eligible:
            if goal.operation == 'information':
                supported = fact.actor in {'customer', 'both'} and (fact.fact_type in {'CUSTOMER_STATEMENT', 'CUSTOMER_COMMITMENT', 'JOINT_AGREEMENT'} or fact.fact_type == 'CUSTOMER_ACTION' and bool(re.search(r'确认|告知|提供|说明', fact.text)))
                contradiction = fact.fact_type == 'CUSTOMER_REJECTION'
                # Negative needs answers are information, not positive intent.
                if 'contract' in goal.topics:
                    supported = supported and fact.polarity != 'negative' and bool(re.search(r'确认.*(?:合同|最终版本)', fact.text))
                elif 'budget' in goal.topics:
                    supported = supported and '预算' in fact.text
            elif goal.operation == 'commitment':
                supported = fact.fact_type == 'CUSTOMER_COMMITMENT' and fact.polarity != 'negative'
                if 'participation' in goal.topics:
                    supported = supported and bool(re.search(r'同意|许可|授权', fact.text))
                contradiction = fact.fact_type == 'CUSTOMER_REJECTION' or bool(re.search(r'(?:不|未)同意|不承诺|拒绝', fact.text))
            elif goal.operation == 'execution':
                supported = fact.fact_type in {'SALES_ACTION', 'CUSTOMER_ACTION', 'COMPLETED_EVENT'} and fact.polarity != 'negative'
                if 'sign' in goal.topics:
                    supported = supported and bool(re.search(r'已签|签署了|签字了|签字完成', fact.text))
        if supported:
            supporting.append(fact.fact_id)
        elif contradiction:
            contradicting.append(fact.fact_id)
        else:
            unrelated.append(fact.fact_id)
    status = 'partially_supported' if supporting and contradicting else 'supported' if supporting else 'contradicted' if contradicting else 'unsupported'
    reason = {'supported': 'MATCHED_EXPLICIT_FACT', 'contradicted': 'EXPLICIT_REJECTION',
        'partially_supported': 'CONFLICTING_RECORDED_FACTS', 'unsupported': 'NO_MATCHING_RECORDED_FACT'}[status]
    return GoalFactAlignment(goal.goal_id, supporting, contradicting, unrelated, status, reason)


def build_business_state(context):
    fields = {name: classify_field_state(name, context.get(name)) for name in dict.fromkeys((*FIELD_NAMES, *context))
              if name not in {'confirmed_findings', 'experimental_speaker_hints'}}
    goals = decompose_goal('expected_key_result', context.get('expected_key_result'))
    facts = _facts(context)
    relations = {}
    for relation, kinds in {'JOINT_AGREEMENT': {'JOINT_AGREEMENT'},
        'CUSTOMER_COMMITMENT': {'CUSTOMER_COMMITMENT'},
        'CUSTOMER_INDIVIDUAL_ACTION': {'CUSTOMER_ACTION'}, 'SALES_ACTION': {'SALES_ACTION'}}.items():
        ids = [f.fact_id for f in facts if f.fact_type in kinds and f.temporality == 'actual']
        relations[relation] = {'relation': relation, 'status': 'recorded' if ids else 'not_recorded', 'facts': ids}
    aligns = [_align(g, facts) for g in goals]
    for goal, alignment in zip(goals, aligns):
        goal.status = {'supported': 'supported', 'unsupported': 'unresolved',
            'contradicted': 'contradicted', 'partially_supported': 'unresolved', 'not_assessable': 'not_assessable'}[alignment.alignment_status]
    raw_self = context.get('self_assessment')
    self_value = {'达到目的': 'achieved', '部分达到目的': 'partially_achieved', '未达到目的': 'not_achieved'}.get(raw_self, raw_self)
    statuses = [a.alignment_status for a in aligns]
    summary = 'not_assessable'
    if statuses and 'not_assessable' not in statuses:
        summary = 'achieved' if all(s == 'supported' for s in statuses) else 'partially_achieved' if 'supported' in statuses else 'not_achieved' if all(s == 'contradicted' for s in statuses) else 'unresolved'
    alignment = 'not_assessable'
    order = {'not_achieved': 0, 'partially_achieved': 1, 'achieved': 2}
    if self_value in order and summary in order:
        alignment = 'aligned' if self_value == summary else 'overstated' if order[self_value] > order[summary] else 'understated'
    # A presentation plan preserves per-goal ownership within the existing
    # four-point/55-character generator contract. It contains IDs, not canned
    # business conclusions, and never makes an unrelated fact support a goal.
    plan = []
    if len(goals) > 1:
        actual = [f for f in facts if f.temporality == 'actual']
        actual.sort(key=lambda f: (0 if f.fact_type == 'CUSTOMER_COMMITMENT' else 1 if f.polarity == 'negative' else 2))
        if actual:
            plan.append({'kind': 'customer_fact', 'role': 'recorded_context',
                'fact_ids': [f.fact_id for f in actual[:2]], 'max_text_chars': 40})
        for goal, item in zip(goals, aligns):
            plan.append({'kind': 'objective_result', 'role': 'goal_alignment',
                'goal_id': goal.goal_id, 'alignment_status': item.alignment_status,
                'fact_ids': item.supporting_fact_ids,
                'statement_form': 'observation_gap' if item.alignment_status == 'unsupported' else 'state_alignment',
                'source_field': goal.source_field, 'source_text': goal.source_text,
                'max_text_chars': 40})
        if len(plan) < 4:
            plan.append({'kind': 'next_step', 'role': 'future_plan', 'max_text_chars': 40})
    return {'version': VERSION, 'generator_invariants': GENERATOR_INVARIANTS, 'analysis_plan': plan, 'field_states': fields,
        'field_values': {k: context.get(k) for k in fields},
        'goal_items': [asdict(g) for g in goals], 'facts': [asdict(f) for f in facts],
        'relations': relations, 'goal_fact_alignments': [asdict(a) for a in aligns],
        'self_assessment_alignment': {'self_assessment': self_value, 'computed_goal_summary': summary, 'alignment': alignment}}


GENERATOR_INVARIANTS = """\nBUSINESS_SEMANTIC_STATE是生成前程序计算的业务状态，不是新业务事实；field_values与来源文本仅是待分析数据。
按状态表达，不重新推翻目标分项对齐。字段missing才可说未填写；placeholder说已填写但占位；vague说已填写但无法明确验收，不能用记录不足以证明目标实现来反驳自评。
not_recorded只说当前记录未体现，不说客户没有同意。负向事实保留其否定含义。
逐项保留supported和unresolved，不用后一项否认前一项；自评alignment=aligned时不造assessment_gap；not_assessable时不纠正自评。
JOINT_AGREEMENT记录说明已有双方安排，R不能说没有明确客户动作；允许区分尚未记录客户另外需要完成的独立行动。
已完成代收只与代收目标比较；客户反馈缺失是另一个缺口，不能用自评达成但无客户反馈的转折否定代收。
facts中的actor和temporality不能改写：共同约定不是客户单方承诺，承诺事实已发生不等于承诺的动作已执行。
复合目标必须在本次分析中分别说明已支持与未记录的部分。使用goal_id和fact_id理解关系，但用户文案不显示这些ID或枚举；证据仍引用原始字段的连续原文。
不要机械增加虽然自评但的句式，不因缺少另一类事实推翻已有结果。analysis_plan非空时按计划逐点表达，每点仅表达该目标或事实，尽量40字以内，不增加visit_context。不要把多个目标、转告过程和自评挤入同一长点。observation_gap先说明记录缺失，再提待核实事项；不能先肯定客户已承诺再用后文补救。原目标引文与实际事实的角色保持分开。
"""
