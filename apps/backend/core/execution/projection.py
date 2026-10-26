"""Deterministic execution read model. Replaying facts never performs work."""
from __future__ import annotations

from copy import deepcopy

from .contracts import ACTIVE, CONTRACT_VERSION, TERMINAL


class ExecutionProjectionError(RuntimeError):
    """An invalid fact prefix must never become permission to execute again."""


def require(condition, message="Invalid execution event prefix"):
    if not condition:
        raise ExecutionProjectionError(message)


def project_event(previous: dict | None, event: dict, root_id: str) -> dict:
    """Apply one event; only the committed prefix decides state and safety."""
    require(event["version"] == CONTRACT_VERSION, "Unsupported execution event version")
    kind, payload = event["kind"], event["payload"]
    require(isinstance(payload, dict))
    if kind == "request.accepted":
        require(previous is None, "Duplicate execution admission")
        identity = payload["identity"]
        require(identity["root_id"] == root_id)
        require(all(identity[key] == event[key] for key in ("turn_id", "run_id", "epoch")))
        require(all(isinstance(identity[key], str) and identity[key] for key in
                    ("session_key", "source", "request_key")))
        require(isinstance(payload["input"], dict))
        return {**identity, "input": payload["input"], "status": "accepted", "result": None,
                "cursor": event["seq"], "last_seq": event["seq"], "effects": {},
                "review": None, "retry_turn_id": None, "retry_of": payload.get("retry_of"),
                "actions": {}, "error_type": None}
    require(previous is not None, "Execution event has no admission fact")
    assert previous is not None
    state = deepcopy(previous)
    require(event["seq"] > state["last_seq"])
    require(all(event[key] == state[key] for key in ("turn_id", "run_id", "epoch")))
    if kind == "run.started":
        require(state["status"] == "accepted")
        state["status"] = "running"
    elif kind in {"run.ended", "run.recovered"}:
        require(state["status"] in ACTIVE and payload.get("status") in TERMINAL)
        require(not unresolved(state) or payload["status"] == "needs_review")
        state.update(status=payload["status"], result=payload.get("result"))
    elif kind == "execution.reviewed":
        require(state["status"] in TERMINAL and state["status"] != "completed")
        require(not state["retry_turn_id"])
        # A later receipt may invalidate a no-effect clearance. Revoke only an
        # unconsumed clearance, preserving both decisions in append-only facts.
        require(state["review"] is None or
                (state["review"]["decision"] == "no_effect" and payload.get("decision") == "close"))
        require(payload.get("decision") in {"close", "no_effect"})
        require(isinstance(payload.get("note"), str) and bool(payload["note"].strip()))
        if payload["decision"] == "no_effect":
            # A successful/confirmed write cannot be relabelled safe by an operator.
            require(not any(e["certainty"] == "confirmed" for e in unsafe_effects(state)))
        state["review"] = payload
        state["actions"][payload["action_id"]] = payload
    elif kind == "execution.retried":
        require(retry_policy(state)["allowed"])
        state["retry_turn_id"] = payload["retry_turn_id"]
        state["actions"][payload["action_id"]] = payload
    elif kind in {"tool.dispatched", "delivery.dispatched"}:
        require(state["status"] == "running")
        effect_id = payload["effect_id"]
        require(isinstance(effect_id, str) and effect_id and effect_id not in state["effects"])
        state["effects"][effect_id] = {**payload, "kind": kind.split(".")[0],
            "dispatch_seq": event["seq"], "certainty": "unknown", "returned": False,
            "retry_safe": kind == "tool.dispatched" and payload.get("retry_policy") == "read_only"}
    elif kind in {"tool.returned", "delivery.returned"}:
        require(state["status"] == "running")
        effect = state["effects"].get(payload["effect_id"])
        require(effect is not None and effect["kind"] == kind.split(".")[0] and not effect["returned"])
        require(payload.get("certainty") in {"unknown", "confirmed"})
        effect.update(certainty=payload["certainty"], returned=True, outcome=payload,
                      return_seq=event["seq"])
        # Adapter proof of rejection is different from a generic tool return.
        if kind == "delivery.returned" and payload["certainty"] == "confirmed" and payload.get("ok") is False:
            effect["retry_safe"] = True
    elif kind in {"reply.delta", "messages.committed", "run.error"}:
        require(state["status"] == "running")
        if kind == "run.error":
            state["error_type"] = payload.get("error_type")
    else:
        raise ExecutionProjectionError(f"Unsupported execution event kind: {kind}")
    state["last_seq"] = event["seq"]
    return state


def unsafe_effects(state: dict) -> list[dict]:
    return [effect for effect in state["effects"].values() if not effect["retry_safe"]]


def unresolved(state: dict) -> bool:
    return any(effect["certainty"] != "confirmed" for effect in unsafe_effects(state))


def retry_policy(state: dict) -> dict:
    if state["status"] not in {"failed", "interrupted", "needs_review"}:
        return {"allowed": False, "reason": "仅失败、中断或待核对的执行可处置"}
    if state["retry_turn_id"]:
        return {"allowed": False, "reason": "已创建重试，请查看关联执行"}
    if state["review"] and state["review"]["decision"] == "close":
        return {"allowed": False, "reason": "已结束处置，保留原执行事实"}
    effects = unsafe_effects(state)
    if effects and not (state["review"] and state["review"]["decision"] == "no_effect"):
        return {"allowed": False, "reason": "存在外部操作；已完成的操作不能随整轮重跑，未知结果须先人工核对"}
    # External transports retain ownership of their own inbox/outbox and receipts.
    if state["source"] != "desktop":
        return {"allowed": False, "reason": "此来源由原渠道队列管理；可核对并结束处置，不从桌面重发"}
    return {"allowed": True, "reason": "将按原输入创建新尝试；原记录保留，使用当前会话上下文"}


def public_state(state: dict) -> dict:
    return {**state, "effects": list(state["effects"].values()), "retry": retry_policy(state),
            "can_review_no_effect": bool(unsafe_effects(state)) and not any(
                e["certainty"] == "confirmed" for e in unsafe_effects(state)) and
                state["review"] is None and not state["retry_turn_id"] and state["status"] in TERMINAL
                and state["status"] != "completed"}
