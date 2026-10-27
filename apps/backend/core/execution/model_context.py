"""Versioned, pure model request projection over an immutable execution prefix.

Observed context components (including legacy history and runtime injections)
are facts. A request selects ordered components; caches and request digests are
derived, never another source of history. No provider or filesystem is read here.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any
from .contracts import CONTRACT_VERSION

VERSION = 1
TRANSPORTS = {"chat.completions.v1", "responses.v1"}


class ModelContextError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def require(condition: bool, message: str):
    if not condition:
        raise ModelContextError(message)


def observe_request(request: dict, transport: str) -> tuple[list[dict], dict]:
    """Freeze the *prepared* semantic request, not mutable caller references.

Each message/tool/parameter is separately addressable. Successive calls select
existing facts where possible; changed prompts/trimmed history become new facts.
The observed messages include the exact resolved memory, skills and hook output.
"""
    require(transport in TRANSPORTS, "Unsupported model transport projection")
    # Authentication is supplied out of band by the transport. Never archive it.
    forbidden = {"api_key", "authorization", "headers", "extra_headers"}
    for parameters in (request, request.get("extra_body", {})):
        require(isinstance(parameters, dict), "Invalid model parameters")
        require(not forbidden.intersection(str(k).lower() for k in parameters),
                "Authentication must not be included in model context facts")
    components: dict[str, dict] = {}
    fields = {}
    for key, value in json.loads(canonical(request)).items():
        many = key in {"messages", "input", "tools"} and isinstance(value, list)
        references = []
        for item in value if many else [value]:
            component = {"field": key, "value": item}
            identity = digest(component)
            components[identity] = {"id": identity, **component}
            references.append(identity)
        fields[key] = {"many": many, "refs": references}
    return list(components.values()), {
        "version": VERSION, "transport": transport, "fields": fields,
        "request_digest": digest(request),
        "provenance": "observed_at_request_boundary",
    }


def materialize(components: dict[str, dict], manifest: dict) -> dict:
    require(manifest.get("version") == VERSION, "Unsupported model context version")
    require(manifest.get("transport") in TRANSPORTS, "Unsupported model transport projection")
    request = {}
    require(isinstance(manifest.get("fields"), dict), "Invalid model context manifest")
    for field, selection in manifest["fields"].items():
        require(type(selection.get("many")) is bool and isinstance(selection.get("refs"), list),
                "Invalid model context selection")
        values = []
        for ref in selection["refs"]:
            component = components.get(ref)
            require(component is not None, "Missing model context fact")
            require(component["field"] == field and digest({"field": field, "value": component["value"]}) == ref,
                    "Model context fact digest mismatch")
            values.append(component["value"])
        require(selection["many"] or len(values) == 1, "Invalid scalar context fact")
        request[field] = values if selection["many"] else values[0]
    require(digest(request) == manifest.get("request_digest"), "Model request digest mismatch")
    validate_tool_pairs(request, manifest["transport"])
    return json.loads(canonical(request))


def validate_tool_pairs(request: dict, transport: str):
    """Pair occurrences, not a global call-id map (IDs can be reused later)."""
    pending: set[str] = set()
    messages = request.get("messages" if transport == "chat.completions.v1" else "input", [])
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls") or []
        if message.get("type") == "function_call":
            calls = [{"id": message.get("call_id")}]
        is_result = message.get("role") == "tool" or message.get("type") == "function_call_output"
        # Tool image observations may be user-content items between parallel
        # results. Pair by durable occurrence, never by a later reused ID.
        for call in calls:
            key = call.get("id")
            require(isinstance(key, str) and bool(key) and key not in pending, "Invalid or overlapping tool call identity")
            assert isinstance(key, str)
            pending.add(key)
        if is_result:
            key = message.get("tool_call_id") if message.get("role") == "tool" else message.get("call_id")
            require(key in pending, "Orphan tool result in model context")
            assert isinstance(key, str)
            pending.remove(key)
    require(not pending, "Model context contains unresolved tool calls")


def project_model_context(events: list[dict], *, root_id: str, turn_id: str,
                          call_id: str, through_seq: int, expected_digest: str | None = None) -> dict:
    """Rebuild a bounded request using facts only. Never schedules any work."""
    require(type(through_seq) is int and through_seq > 0, "Invalid model context prefix")
    prefix = [e for e in events if e["seq"] <= through_seq]
    require(bool(prefix) and prefix[-1]["seq"] == through_seq, "Model context prefix is missing")
    require(prefix[0]["kind"] == "request.accepted", "Missing execution admission")
    identity = prefix[0]["payload"]["identity"]
    require(identity["root_id"] == root_id and identity["turn_id"] == turn_id, "Context identity mismatch")
    previous = 0
    components = {}
    selected = None
    selected_components = None
    for event in prefix:
        require(event['version'] == CONTRACT_VERSION, 'Unsupported execution fact version')
        require(event["seq"] > previous, "Unordered context prefix")
        require(all(event[k] == identity[k] for k in ("turn_id", "run_id", "epoch")), "Mixed context identities")
        previous = event["seq"]
        if event["kind"] == "model.context.observed":
            payload = event["payload"]
            require(payload.get("version") == VERSION, "Unsupported context fact version")
            for component in payload["components"]:
                key = component["id"]
                require(digest({"field": component["field"], "value": component["value"]}) == key,
                        "Model context fact digest mismatch")
                require(key not in components or components[key] == component, "Conflicting context facts")
                components[key] = component
        elif event["kind"] == "model.request.prepared" and event["payload"]["call_id"] == call_id:
            require(selected is None, "Duplicate model request fact")
            before = [e for e in prefix if e['seq'] < event['seq']]
            require(event['payload'].get('input_prefix_digest') == digest({'root_id': root_id, 'events': before}),
                    'Model input event prefix is incomplete or corrupted')
            known = {e['seq'] for e in before if e['kind'] in {
                'model.history.selected', 'model.response.recorded', 'interaction.tool_result'}}
            require(set(event['payload'].get('derived_from', [])).issubset(known), 'Missing semantic source')
            selected = event
            selected_components = dict(components)
    require(selected is not None, "No recorded model context for this call (legacy history is not an exact request)")
    prefix_digest = digest({"root_id": root_id, "events": prefix})
    require(expected_digest is None or prefix_digest == expected_digest, "Model context prefix digest mismatch")
    assert selected is not None and selected_components is not None
    request = materialize(selected_components, selected["payload"])
    return {"version": VERSION, "root_id": root_id, "turn_id": turn_id,
            "call_id": call_id, "through_seq": through_seq, "prefix_digest": prefix_digest,
            "request_seq": selected["seq"], "transport": selected["payload"]["transport"],
            "request_digest": digest(request), "request": request}


def project_interaction_history(events: list[dict], root_id: str) -> dict:
    """Semantic model steps, not UI text or diagnostic/internal model calls.

An unresolved tool step is retained in the ledger and explicitly excluded from
new-turn history. This is not permission to resume/retry the old execution.
"""
    messages, diagnostics, calls, results = [], [], {}, {}
    if events:
        require(events[0]['kind'] == 'request.accepted', 'Missing interaction admission')
        identity = events[0]['payload']['identity']
        require(identity['root_id'] == root_id, 'Interaction root mismatch')
        previous = 0
        for event in events:
            require(event['version'] == CONTRACT_VERSION and event['seq'] > previous, 'Invalid interaction event order/version')
            require(all(event[k] == identity[k] for k in ('turn_id', 'run_id', 'epoch')), 'Mixed interaction identities')
            previous = event['seq']
    for event in events:
        payload = event['payload']
        if event['kind'] == 'model.request.prepared':
            calls[payload['call_id']] = payload
        elif event['kind'] == 'interaction.tool_result':
            results[(payload['call_id'], payload['tool_call_id'])] = payload['messages']
    for event in events:
        if event['kind'] != 'model.response.recorded':
            continue
        payload = event['payload']
        call = calls.get(payload['call_id'], {})
        if not call.get('history_visible') or 'response' not in payload:
            continue
        response = payload['response']
        tool_calls = response.get('tool_calls', [])
        if any((payload['call_id'], c['id']) not in results for c in tool_calls):
            diagnostics.append({'call_id': payload['call_id'], 'reason': 'unresolved_tool_step_excluded',
                                'automatic_retry': False})
            continue
        message = {'role': 'assistant', 'content': response.get('content')}
        message.update(response.get('provider_fields') or {})
        if tool_calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {
                'name': c['name'], 'arguments': json.dumps(c['arguments'], ensure_ascii=False)}} for c in tool_calls]
        messages.append(message)
        for call_result in tool_calls:
            messages.extend(results[(payload['call_id'], call_result['id'])])
    return {'messages': json.loads(canonical(messages)), 'diagnostics': diagnostics,
            'source': {'root_id': root_id, 'turn_id': events[0]['turn_id'] if events else None,
                       'through_seq': events[-1]['seq'] if events else 0,
                       'prefix_digest': digest(events)}}
