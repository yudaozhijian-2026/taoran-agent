"""V36 bounded invariants over computed state; no LLM or business scoring."""
import re

FIELD_LABELS = {
    'expected_key_result': r'(?:本次)?(?:目标|关键结果|预期结果)',
    'next_action_expected_result': r'(?:下次|下一步|下次拜访|下次行动)?(?:期望结果|期望的关键结果|预期结果)',
    'purpose_code': r'(?:本次)?拜访目的', 'other_purpose': r'(?:本次)?其他目的',
    'next_action_purpose': r'(?:下次|下一步|下次拜访)(?:行动)?目的',
    'next_action_other_purpose': r'(?:下次|下一步)(?:行动)?其他目的',
    'deviation_reason': r'(?:偏差|偏离)原因',
    'next_contact_at': r'(?:下次|下一次)?(?:联系|拜访)(?:时间|日期)',
    'process_description': r'过程(?:描述|记录|详细描述)?',
    'customer_feedback': r'客户反馈', 'self_assessment': r'自评',
    'visit_date': r'(?:本次)?拜访日期', 'customer_type_ii': r'客户类型',
    'visit_method': r'拜访方式', 'is_appointment': r'预约(?:状态)?',
    'opportunity_stage': r'商机阶段', 'opportunity_stages': r'商机阶段',
}
# Explicit scope wins over shorter legacy aliases. Equal ambiguous aliases
# remain unassigned; neither an adjacent field nor an entire sentence owns them.
_SCOPED_FIELD = re.compile(
    r'(?P<scope>本次|此次|下一次|下一步|下次)(?:拜访|行动|跟进)?(?:的)?'
    r'(?P<label>期望的关键结果|期望结果|预期结果|关键结果|其他目的|联系时间|联系日期|拜访时间|拜访日期|目标|目的|日期|时间)'
)


def field_references(clause):
    candidates = []
    for field, pattern in FIELD_LABELS.items():
        candidates.extend((m.start(), m.end(), field, 0) for m in re.finditer(pattern, clause))
    for m in _SCOPED_FIELD.finditer(clause):
        future = m['scope'] in {'下一次', '下一步', '下次'}
        label = m['label']
        if label in {'目标', '关键结果', '期望结果', '期望的关键结果', '预期结果'}:
            field = 'next_action_expected_result' if future else 'expected_key_result'
        elif label == '其他目的':
            field = 'next_action_other_purpose' if future else 'other_purpose'
        elif label == '目的':
            field = 'next_action_purpose' if future else 'purpose_code'
        else:
            field = 'next_contact_at' if future else 'visit_date'
        candidates.append((m.start(), m.end(), field, 1))
    accepted = []
    for start, end, field, priority in sorted(candidates, key=lambda x: (-x[3], -(x[1]-x[0]), x[0])):
        if any(start < b and a < end for a, b, _, _ in accepted):
            continue
        peers = {f for a, b, f, pr in candidates if a == start and b == end and pr == priority}
        accepted.append((start, end, field if len(peers) == 1 else None, priority))
    return [(a, b, f) for a, b, f, _ in sorted(accepted)]


def field_claims(text):
    """Yield one locally owned field assertion at a time."""
    for clause in re.split(r'[，,。！？；;\n]|且(?=未|尚未)|并(?=未|尚未)', text):
        refs = field_references(clause)
        for index, (start, end, field) in enumerate(refs):
            if field is None:
                continue
            stop = refs[index + 1][0] if index + 1 < len(refs) else len(clause)
            # Include a directly preceding missing predicate, not previous prose.
            before = clause[refs[index - 1][1] if index else 0:start]
            prefix = re.search(r'(?:尚未填写|未(?:具体)?填写|没有填写|未提供|未录入)(?:具体)?$', before) if index == 0 else None
            yield field, (prefix.group(0) if prefix else '') + clause[start:stop]


def unrecorded_denials(text, state, goal_id=None):
    """Reject asserted denials only when their goal referent is determined."""
    unresolved = [g for g in state['goal_items'] if g['status'] == 'unresolved']
    errors = []
    topic_words = {'conditions': r'条件|反馈具体', 'sign': r'签署|签字', 'contract': r'合同|版本',
                   'referral': r'引荐', 'order': r'下单|订单', 'needs': r'需求'}
    denial = re.compile(r'客户(?:并未|没有|尚未|未)(?:就此|对此|就该事项)?(?:明确)?(?:作出|做出)?(?:明确)?(?:承诺|同意|确认|签署)')
    for sentence in re.split(r'[。！？；;\n]', text):
        previous = ''
        for clause in re.split(r'[，,]', sentence):
            for match in denial.finditer(clause):
                prefix = clause[:match.start()]
                if re.search(r'记录.{0,12}(?:未|无|不足)|未(?:体现|记录|显示|提及)|是否|无法判断|不能说|不应说|不得说', prefix):
                    continue
                selected = [g for g in unresolved if g['goal_id'] == goal_id] if goal_id else [
                    g for g in unresolved if any(re.search(topic_words[t], clause + previous)
                    for t in g['topics'] if t in topic_words)]
                if not goal_id and not selected and len(unresolved) == 1 and re.search(r'就此|对此|就该事项', match.group(0)):
                    selected = unresolved
                if len(selected) != 1:
                    continue
                goal = selected[0]
                negative = [f for f in state['facts'] if f['source_field'] in {'process_description', 'customer_feedback'}
                    and f['actor'] == 'customer' and f['temporality'] == 'actual' and f['record_status'] == 'negative_fact'
                    and set(f['topics']) & set(goal['topics'])
                    and re.search(r'拒绝|不(?:会|愿|同意)|未(?:作出|承诺)|没有承诺', f['text'])]
                if not negative:
                    errors.append({'code': 'NOT_RECORDED_AS_NEGATIVE_FACT', 'text': clause,
                                   'goal_ids': [goal['goal_id']], 'fact_ids': []})
            previous = clause
    return errors


_MISSING = r'(?:尚未填写|未填写|没有填写|未提供|未录入|为空|空白)'
_NEGATION = r'(?:未|没有|尚未|尚无|并未|无法|不能)'
_UNCERTAIN = re.compile(r'(?:记录|原文|表单|内容).{0,8}(?:未|没有|不足|不够)|未(?:体现|记录|提及)|尚不清楚|无法判断')
# Bind negation to the action noun; never scan across an unrelated verb such
# as 未深入交流 into a later 已约定 clause, including comma/ideographic lists.
_DENY_ACTION = re.compile(
    r'(?:没有|尚无|缺少)(?:任何|明确|具体|客观)?(?:客户)?(?:表达或)?(?:动作|行动|约定|安排|互动)'
    r'|未(?:记录|体现|写明|说明|形成)(?:任何|明确|具体|客观)?(?:客户)?(?:动作|行动|约定|安排)'
    r'|未(?:写明|说明)[^，,、：:]{0,24}(?:作出|做出)[^，,、：:]{0,10}(?:动作|行动)'
)

_DOWNGRADE = re.compile(r'(?:目标|代收|收货|采购沟通).{0,18}(?:未达成|没达成|未完成|不成立)|(?:不足以证明|不能认为|不能认定|不能证明|尚不能认为).{0,35}(?:目标|代收|收货|采购沟通).{0,12}(?:实现|达成|完成)|(?:两个|两项|所有|各个)(?:子)?目标.{0,8}(?:均|都).{0,6}部分')
_TRANSFER_DONE = re.compile(r'(?:已|已经)(?:将|把)?[^，,。；]{0,16}(?:转交|转发|转告|发给|反馈给)')


def validate_invariants(text, state, *, require_goal_coverage=False, bindings=None):
    from .experimental_rendering_binding import text_invariants, validate_bindings
    errors = text_invariants(text, state) + unrecorded_denials(text, state)
    if bindings is not None:
        errors += validate_bindings(bindings, state)
    fields = state['field_states']
    facts = state['facts']
    goals = state['goal_items']
    align = state['self_assessment_alignment']
    joint = state['relations']['JOINT_AGREEMENT']['status'] == 'recorded'
    supported = [g for g in goals if g['status'] == 'supported']
    def fail(code, fragment, *, goal_ids=None, fact_ids=None, field=None):
        item = {'code': code, 'text': fragment, 'goal_ids': goal_ids or [], 'fact_ids': fact_ids or []}
        if field:
            item.update(field=field, field_state=fields.get(field))
        if item not in errors:
            errors.append(item)
    for sentence in re.split(r'[。！？；;\n]', text):
        if not sentence.strip():
            continue
        for field, fragment in field_claims(sentence):
            if fields.get(field) in {'placeholder', 'vague', 'assessable'} and re.search(_MISSING, fragment):
                fail('FIELD_STATE_CONTRADICTION', fragment, field=field)
        if fields.get('expected_key_result') in {'vague', 'placeholder', 'missing'}:
            if _DOWNGRADE.search(sentence) and (re.search(r'目标.{0,10}(?:未达成|没达成|未完成)', sentence) or not re.search(r'拉近|保持|争取|收集|推进|实现', str(state['field_values'].get('expected_key_result') or ''))) and not re.search(r'不能因|不得因|不影响|无法.{0,10}(?:判断|确定)', sentence):
                fail('FIELD_STATE_CONTRADICTION', sentence, field='expected_key_result')
            if fields.get('expected_key_result') in {'placeholder', 'missing'} and re.search(r'目标(?:为|是).*(?:催款|获得参与|收货)', sentence) and not re.search(r'不能|不得|并非|不是', sentence):
                fail('FIELD_STATE_CONTRADICTION', sentence, field='expected_key_result')
        if joint and (_DENY_ACTION.search(sentence) or re.search(r'没有客户表达[^，,]{0,8}或动作', sentence)) and not re.search(r'另(?:外|行)|独立动作|独立行动|不能说|不等于|不得', sentence):
            fail('JOINT_AGREEMENT_ERASED', sentence, fact_ids=state['relations']['JOINT_AGREEMENT']['facts'])
        if (any('transfer' in f['topics'] and f['temporality'] == 'planned' for f in facts)
                and _TRANSFER_DONE.search(sentence)
                and not re.search(r'已(?:经)?(?:承诺|答应|表示)|(?:是否|未体现|未记录).{0,8}已', sentence)
                and not any('transfer' in f['topics'] and f['temporality'] == 'actual'
                    and f['fact_type'] in {'CUSTOMER_ACTION', 'COMPLETED_EVENT', 'SALES_ACTION'} for f in facts)):
            fail('TEMPORALITY_MISMATCH', sentence)
        # Same recognizable action, different explicitly resolved owner.
        for action in ('介绍', '催款', '代收'):
            own = [f for f in facts if action in f['text'] and f['actor'] == 'sales' and f['temporality'] == 'actual']
            if own and re.search(r'(?<!向)(?<!给)客户(?:已|已经)?' + action, sentence) and not re.search(r'未体现|没有|不能|不得|是否', sentence):
                fail('ACTOR_MISMATCH', sentence, fact_ids=[f['fact_id'] for f in own])
        for goal in goals:
            topics = goal['topics']
            if goal['status'] == 'unresolved' and 'conditions' in topics:
                for clause in re.split(r'[，,]', sentence):
                    if re.search(r'(?:未|没有|尚未)(?:明确)?(?:同意|承诺).{0,8}(?:反馈|条件)', clause) and not re.search(r'未(?:体现|记录|显示|提及|见)|尚不清楚|无法判断', clause):
                        fail('NOT_RECORDED_AS_NEGATIVE_FACT', clause, goal_ids=[goal['goal_id']])
            if goal['status'] == 'supported' and 'needs' in topics and re.search(r'(?:尚未|没有|未).{0,5}确认.{0,8}(?:合作)?需求|(?:两个|两项)目标.{0,8}(?:都|均).{0,8}部分', sentence):
                fail('SUPPORTED_GOAL_DOWNGRADED', sentence, goal_ids=[goal['goal_id']])
        # One V362 rule: cooperation attitude is not a completion condition
        # for an information-only procurement communication goal. Do not change
        # the frozen builder's unresolved alignment into supported here.
        procurement_information = [g for g in goals if g['operation'] == 'information' and 'procurement' in g['topics']]
        information_facts = [f for f in facts if f['temporality'] == 'actual' and f['actor'] == 'customer' and set(f['topics']) & {'budget', 'procurement'}]
        if (procurement_information and information_facts
                and re.search(r'合作意愿.{0,8}(?:中立|不明确|不强|较低).{0,16}(?:尚不能认为|不能认定|不足以证明|所以|因此).{0,10}采购沟通(?:目标)?.{0,8}(?:完成|达成)', sentence)
                and not re.search(r'不能因|不得因|不应因|不影响|不等于', sentence)):
            fail('PROCUREMENT_ATTITUDE_AS_COMPLETION_CONDITION', sentence,
                 goal_ids=[g['goal_id'] for g in procurement_information], fact_ids=[f['fact_id'] for f in information_facts])
        if supported and _DOWNGRADE.search(sentence) and not re.search(r'不影响|不能因|不得因|不能以|尚未全部|未全部', sentence):
            fail('SUPPORTED_GOAL_DOWNGRADED', sentence, goal_ids=[g['goal_id'] for g in supported])
        receipt = [g for g in supported if 'receipt' in g['topics']]
        if receipt and re.search(r'自评.{0,12}(?:但|然而).{0,50}客户.{0,10}(?:接收|签收|反馈)|自评.{0,12}但.{0,12}(?:缺少|未记录|未体现)客户', sentence):
            fail('CROSS_GOAL_CONTAMINATION', sentence, goal_ids=[g['goal_id'] for g in receipt])
        if supported and align['alignment'] == 'aligned' and re.search(r'自评.{0,10}部分.{0,8}但', sentence) and re.search(r'不足以证明|未实现|未达成', sentence):
            fail('CROSS_GOAL_CONTAMINATION', sentence, goal_ids=[g['goal_id'] for g in supported])
        if fields.get('next_contact_at') == 'missing' and re.search(r'(?:未|没有|尚未)(?:约定|安排|确定).{0,8}(?:时间|日期)', sentence) and not _UNCERTAIN.search(sentence):
            fail('NOT_RECORDED_AS_NEGATIVE_FACT', sentence, field='next_contact_at')
    if bindings is None and require_goal_coverage and align['computed_goal_summary'] == 'partially_achieved':
        # Check only the recognizable supported/unresolved relation; this is not
        # an open-ended completeness or writing-style reviewer.
        for goal in goals:
            if 'needs' in goal['topics'] and goal['status'] == 'supported' and not re.search(r'(?:需求|状态|信息|采购计划|二供计划|增供计划).{0,10}(?:确认|已获|支持|支撑)|(?:确认|已获).{0,10}(?:需求|状态|信息|采购计划|二供计划|增供计划)|前半部分.{0,8}(?:支持|支撑)', text):
                fail('GOAL_ALIGNMENT_OMITTED', text, goal_ids=[goal['goal_id']])
            if 'conditions' in goal['topics'] and goal['status'] == 'unresolved' and not re.search(r'(?:反馈|条件|承诺).{0,16}(?:未体现|未记录|尚待|待确认)|(?:未体现|未记录|尚未体现|尚未记录).{0,18}(?:反馈|条件|承诺)', text):
                fail('GOAL_ALIGNMENT_OMITTED', text, goal_ids=[goal['goal_id']])
    return errors
