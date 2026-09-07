"""Candidate-only bound rendering validation and contract-scoped repair."""
import re
from copy import deepcopy

from .experimental_rendering_guidance import next_action_fact_ids, rendering_input
from .experimental_semantic_invariants import field_claims, unrecorded_denials


def text_invariants(text, state):
    errors = []
    def fail(code, fragment, field=None):
        errors.append({'code': code, 'text': fragment, 'field': field})
    fields = state['field_states']
    for sentence in re.split(r'[。！？；;\n]', text):
        for field, fragment in field_claims(sentence):
            if fields.get(field) == 'placeholder' and re.search(r'未(?:具体)?填写|没有填写|为空|空白', fragment):
                fail('PLACEHOLDER_AS_MISSING', fragment, field)
            if fields.get(field) == 'missing' and re.search(r'已填写.{0,6}不具体', fragment):
                fail('MISSING_AS_VAGUE', fragment, field)
        goal_unassessable = fields.get('expected_key_result') in {'missing','placeholder','not_received'}
        self_unassessable = goal_unassessable
        if goal_unassessable:
            target = str(state['field_values'].get('expected_key_result') or '')
            target_ref = '目标' in sentence or (target and target in sentence) or bool(re.search('关系.{0,5}拉近|拉近.{0,5}关系', sentence) and '关系' in target)
            if target_ref and re.search(r'不足以证明|不能证明|尚不能证明|未达成|未实现|未完成', sentence) and not re.search(r'不能因|不得因|不应因|无法.{0,8}判断', sentence):
                fail('NOT_ASSESSABLE_DOWNGRADED', sentence, 'expected_key_result')
            if re.search(r'自评.{0,10}(?:缺少.{0,5}支撑|缺乏.{0,5}依据)|自评.{0,15}但.{0,15}(?:未体现|未记录|缺少|没有)客户反馈', sentence):
                fail('NOT_ASSESSABLE_SELF_CORRECTION', sentence, 'expected_key_result')
        if goal_unassessable or self_unassessable:
            direct = re.search(r'自评.{0,8}(?:错误|偏乐观|过高|不符|不一致)|(?:应|建议|需要|需).{0,8}(?:调整|修改|改为|改成).{0,12}(?:部分达成|未达成)|自评.{0,8}(?:应|建议).{0,8}(?:部分|未)达成', sentence)
            if direct and not re.search(r'不得|不能|不应|不建议|不要|不足以判断|无法判断|不能判断', sentence):
                fail('NOT_ASSESSABLE_SELF_CORRECTION', sentence, 'self_assessment')
    return errors


FACT_REQUIRED_CLAIMS = frozenset({'supported', 'achieved', 'joint_agreement_recorded', 'completed_event', 'negative_fact', 'contradicted'})


def resolve_binding_fact_ids(contract, semantic_state):
    """Authoritative IDs from immutable state, never model-supplied metadata."""
    claim = contract.get('claim_type') or contract['allowed_claim_types'][0]
    goal_id = contract.get('goal_id')
    facts = semantic_state['facts']
    if goal_id:
        # Semantic support comes from the model's validated source proofs,
        # never from keyword alignment. Do not auto-attach guessed support IDs.
        return []
    if claim in {'unresolved', 'not_recorded', 'not_assessable', 'placeholder', 'vague', 'missing'}:
        return []
    if contract['contract_id'] == 'C_NEXT':
        return next_action_fact_ids(semantic_state)
    if contract['contract_id'] == 'C_PROCESS':
        if claim == 'joint_agreement_recorded':
            return list(semantic_state['relations']['JOINT_AGREEMENT']['facts'])
        scoped = [f for f in facts if f['source_field'] in {'process_description', 'customer_feedback'}]
        if claim == 'completed_event':
            scoped = [f for f in scoped if f['temporality'] == 'actual' and f['fact_type'] in {'COMPLETED_EVENT', 'CUSTOMER_ACTION', 'SALES_ACTION'}]
        if claim == 'negative_fact':
            scoped = [f for f in scoped if f['record_status'] == 'negative_fact']
        return [f['fact_id'] for f in scoped]
    return []


def resolve_bindings(points, state):
    contracts = {c['contract_id']: c for c in rendering_input(state)['RENDERING_CONTRACTS']}
    result = []
    for point in points:
        c = contracts.get(point.get('contract_id'))
        if not c:
            continue
        fact_ids = resolve_binding_fact_ids({**c, 'claim_type': point.get('claim_type')}, state)
        result.append({'contract_id': c['contract_id'], 'goal_id': c['goal_id'], 'claim_type': point.get('claim_type'),
            'fact_ids': fact_ids, 'source_fields': sorted({f['source_field'] for f in state['facts'] if f['fact_id'] in fact_ids}),
            'allowed_fact_ids': c['allowed_fact_ids'], 'supporting_fact_ids': c['supporting_fact_ids'],
            'contradicting_fact_ids': c['contradicting_fact_ids'], 'relation_state': c['relation_state']})
    return result


def normalize_bindings(points, state):
    """Fill only uniquely determined identifiers; never infer a claim from prose."""
    result = deepcopy(points)
    if not isinstance(result, list):
        return result
    contracts = rendering_input(state)['RENDERING_CONTRACTS']
    by_id = {c['contract_id']: c for c in contracts}
    sections = {'customer_fact': 'C_PROCESS', 'next_step': 'C_NEXT', 'visit_context': 'C_CONTEXT'}
    for point in result:
        if not isinstance(point, dict):
            continue
        cid, gid = point.get('contract_id'), point.get('goal_id')
        if not cid:
            candidates = [c for c in contracts if gid and c['goal_id'] == gid]
            if not gid:
                section = sections.get(point.get('kind'))
                if point.get('kind') == 'objective_result' and 'C_GOAL_FIELD' in by_id:
                    section = 'C_GOAL_FIELD'
                candidates = [c for c in contracts if c['contract_id'] == section]
            if len(candidates) == 1:
                cid = point['contract_id'] = candidates[0]['contract_id']
        # An explicit wrong identifier remains visible to the validator.
        if cid in by_id and not gid and by_id[cid]['goal_id']:
            point['goal_id'] = by_id[cid]['goal_id']
    return result


def experimental_retry_allowed(context):
    """Retry semantic corrections only, never ambiguous/malformed binding."""
    rejection = (context or {}).get('rejection', {})
    errors = rejection.get('binding_errors', [])
    terminal = {'BINDING_SHAPE', 'UNKNOWN_CONTRACT',
                'REQUIRED_CONTRACT_OMITTED', 'GOAL_ALIGNMENT_OMITTED',
                'BOUND_TEXT_MISSING',
                'STATE_SUPPORT_MISSING', 'RETRY_OUT_OF_SCOPE'}
    if any(e.get('code') == 'DUPLICATE_CONTRACT' and e.get('contract_id') != 'C_PROCESS' for e in errors):
        return False
    if errors:
        targets = {t['failed_contract_id'] for t in rejection.get('rendering_repairs', [])}
        return bool(targets) and all(e.get('contract_id') in targets and e['code'] not in terminal for e in errors)
    return bool(rejection.get('invariant_errors') or rejection.get('state_errors')
                or rejection.get('semantic_issues'))


def validate_bindings(points, state, *, retained_texts=None):
    contracts = {c['contract_id']: c for c in rendering_input(state)['RENDERING_CONTRACTS']}
    errors, seen = [], set()
    def fail(code, point, contract=None):
        errors.append({'code': code, 'text': point.get('text', ''),
                       'contract_id': (contract or {}).get('contract_id', point.get('contract_id')),
                       'goal_ids': [contract['goal_id']] if contract and contract['goal_id'] else []})
    if not isinstance(points, list):
        return [{'code': 'BINDING_SHAPE', 'text': '', 'contract_id': None}]
    for point in points:
        if not isinstance(point, dict):
            fail('BINDING_SHAPE', {})
            continue
        contract = contracts.get(point.get('contract_id'))
        if not contract:
            fail('UNKNOWN_CONTRACT', point)
            continue
        cid = contract['contract_id']
        # A process can contain several independent, separately grounded facts.
        # Keep every fragment under the same structural group; do not rewrite
        # their actors/states or combine their proof coverage.
        if cid in seen and cid != 'C_PROCESS':
            fail('DUPLICATE_CONTRACT', point, contract)
        seen.add(cid)
        if point.get('goal_id', '') != contract['goal_id'] or point.get('claim_type') not in contract['allowed_claim_types']:
            fail('BINDING_STATE_MISMATCH', point, contract)
        text = point.get('text')
        if not isinstance(text, str) or not text.strip():
            fail('BOUND_TEXT_MISSING', point, contract)
            continue
        if retained_texts is not None and text.strip().rstrip('。！？') not in {t.strip().rstrip('。！？') for t in retained_texts}:
            fail('BOUND_TEXT_DROPPED', point, contract)
        # Metadata comes from state, not model output. Original proof parsing
        # still validates quotes later; no extra fact-to-proof bijection.
        fact_ids = resolve_binding_fact_ids({**contract, 'claim_type': point.get('claim_type')}, state)
        if not contract['goal_id'] and point.get('claim_type') in FACT_REQUIRED_CLAIMS and not fact_ids:
            fail('STATE_SUPPORT_MISSING', point, contract)
        proofs = point.get('proofs', [])
        if contract['goal_id'] and point.get('claim_type') in {'supported','partially_supported','contradicted'} and not any(
            isinstance(p,dict) and p.get('field') in {'process_description','customer_feedback'} and p.get('quote') for p in proofs if isinstance(proofs,list)):
            fail('STATE_SUPPORT_MISSING', point, contract)
        if not isinstance(proofs, list) or any(not isinstance(p, dict) or p.get('field') not in contract['source_fields'] for p in proofs):
            fail('BOUND_PROOF_MISMATCH', point, contract)
        if contract['allowed_claim_types'] == ['not_assessable'] and not (re.search(r'标准|验收|关键结果不明确', text) and re.search(r'无法|难以|不能判断', text)):
            fail('NOT_ASSESSABLE_WORDING_OMITTED', point, contract)
        if cid == 'C_NEXT' and contract['semantic_state']['state'] == 'placeholder' and not (re.search(r'占位', text) and re.search(r'已填|填写|填了', text)):
            fail('PLACEHOLDER_WORDING_OMITTED', point, contract)
        if contract['goal_id']:
            goal = contract['semantic_state']['goal']
            # Verify the bound subject, not whether particular uncertainty
            # keywords occur. This catches swapped G1/G2 labels with good types.
            anchors = {'needs': r'需求|采购计划|二供计划|增供计划', 'conditions': r'条件',
                       'receipt': r'代收|收货', 'sign': r'签署|签字', 'contract': r'合同|版本',
                       'budget': r'预算', 'referral': r'引荐', 'order': r'下单|点单'}
            topics = [t for t in goal['topics'] if t in anchors]
            if topics and not all(re.search(anchors[t], text) for t in topics if t not in {'contract'} or 'sign' not in topics):
                fail('GOAL_TEXT_BINDING_MISMATCH', point, contract)
            for error in unrecorded_denials(text, state, contract['goal_id']):
                fail(error['code'], {**point, 'text': error['text']}, contract)
        if cid == 'C_PROCESS' and contract['allowed_claim_types'] == ['joint_agreement_recorded'] and not re.search(r'约定|共同安排|商定|约好', text):
            fail('JOINT_AGREEMENT_OMITTED', point, contract)
        for error in text_invariants(text, state):
            fail(error['code'], {**point, 'text': error['text']}, contract)
    for contract in contracts.values():
        if contract['required'] and contract['contract_id'] not in seen:
            fail('GOAL_ALIGNMENT_OMITTED' if contract['goal_id'] else 'REQUIRED_CONTRACT_OMITTED', {}, contract)
    return errors


def repair_targets(errors, state):
    data = rendering_input(state)
    targets = []
    for error in errors:
        for c in data['RENDERING_CONTRACTS']:
            if (c['contract_id'] == error.get('contract_id') or c['goal_id'] and c['goal_id'] in error.get('goal_ids', [])
                    or not error.get('contract_id') and error.get('field') in c['source_fields'] or 'suggestion:' + next(iter(c['suggestion_scope']), '-') in error.get('targets', [])):
                targets.append({'failed_contract_id': c['contract_id'], 'failed_goal_id': c['goal_id'], 'expected_goal_id': c['goal_id'],
                    'expected_claim_type': c['allowed_claim_types'], 'forbidden_claim': error.get('text', ''),
                    'allowed_source_fields': c['source_fields'],
                    'repair_components': ['text', 'claim_type', 'proofs'] if error['code'] in {'BOUND_PROOF_MISMATCH', 'NUMERIC_EVIDENCE_MISSING'} else ['text', 'goal_id', 'claim_type'],
                    'canonical_wording': [word for g in data['KNOWLEDGE_GUIDANCE'] if g['guidance_code'] in c['knowledge_guidance_codes'] for word in g['allowed_language']],
                    'forbidden_wording': error.get('text', ''), 'error_code': error['code'], 'semantic_state': deepcopy(c['semantic_state']),
                    'knowledge_guidance': [g for g in data['KNOWLEDGE_GUIDANCE'] if g['guidance_code'] in c['knowledge_guidance_codes']],
                    'suggestion_scope': c['suggestion_scope']})
    return targets


def check_targeted_retry(payload, repair_context):
    """Reject changes to non-targeted contracts/items in the single retry."""
    rejection = (repair_context or {}).get('rejection', {})
    targets = rejection.get('rendering_repairs', [])
    previous = (repair_context or {}).get('previous_candidate', {})
    if not targets:
        return []
    allowed = {t['failed_contract_id'] for t in targets}
    scopes = {s for t in targets for s in t['suggestion_scope']}
    errors = []
    for key, identity, permitted in [('analysis_points', 'contract_id', allowed), ('items', 'code', scopes)]:
        old = previous.get(key, [])
        new = payload.get(key, [])
        # Preserve unresolved positions independently. Never collapse them into None.
        def identities(rows, identity=identity):
            counts = {}
            identities = []
            for index, point in enumerate(rows):
                value = point.get(identity) if isinstance(point, dict) else None
                if not value:
                    identities.append(('position', index))
                else:
                    count = counts.get(value, 0)
                    counts[value] = count + 1
                    identities.append(('id', value, count))
            return identities
        old_keys, new_keys = identities(old), identities(new)
        current = dict(zip(new_keys, new))
        for index, point in enumerate(old):
            value = point.get(identity) if isinstance(point, dict) else None
            if value not in permitted and _model_owned(current.get(old_keys[index])) != _model_owned(point):
                errors.append({'code': 'RETRY_OUT_OF_SCOPE', 'contract_id': value if key == 'analysis_points' else None,
                               'point_index': index, 'text': point.get('text', '') if isinstance(point, dict) else ''})
        for index, point in enumerate(new):
            value = point.get(identity) if isinstance(point, dict) else None
            if new_keys[index] not in old_keys and value not in permitted:
                errors.append({'code': 'RETRY_OUT_OF_SCOPE', 'contract_id': value if key == 'analysis_points' else None,
                               'point_index': index, 'text': point.get('text', '') if isinstance(point, dict) else ''})
    return errors


def _model_owned(point):
    if not isinstance(point, dict):
        return point
    return {k: v for k, v in point.items() if k not in {'fact_ids', 'source_fields', 'allowed_fact_ids', 'supporting_fact_ids', 'contradicting_fact_ids', 'relation_state'} and not (k == 'goal_id' and v == '')}
