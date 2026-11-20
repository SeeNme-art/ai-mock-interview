"""EXP-002 准备：离线生成请求清单、估算费用并校验合成响应；没有网络客户端。"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

PRICE = {
    "provider": "DeepSeek", "model": "deepseek-flash", "currency": "USD",
    "checked_on": "2026-11-19", "input_cache_miss_per_million": "0.3",
    "output_per_million": "1.2", "basis": "peak_rate_no_cache_discount",
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
}
ANSWER = "我用固定查询比较了两种分块大小。"
CASES = [
    {"id": "connection_json", "prompt": '仅输出 JSON 对象 {"ok":true}。', "fixture": {"ok": True}},
    {"id": "extract_profile", "prompt": '合成材料：掌握 Python，做过课程检索练习。提取为 JSON，字段 skills(字符串数组)、project(名称字符串)，不补经历。',
     "fixture": {"skills": ["Python"], "project": "课程检索练习"}},
    {"id": "rewrite_question", "prompt": '合成背景：做过课程检索练习。围绕如何验证召回效果问一个问题，不编造事实。仅输出 JSON {"question":"问题"}。',
     "fixture": {"question": "你会怎样验证课程检索练习的召回效果？"}},
    {"id": "probe", "prompt": '合成问题：如何验证检索效果？回答：感觉更准。决定是否澄清或深挖。仅输出 JSON，字段 should_probe(boolean)、purpose(clarify/deepen/check_consistency 或 null)、question(字符串或 null)。不追问时后两项为 null。',
     "fixture": {"should_probe": True, "purpose": "deepen", "question": "你用什么样本和指标比较效果？"}},
    {"id": "score_evidence", "prompt": '合成回答：' + ANSWER + ' 仅检查是否有验证方法，按0至4整数评价，不合成总分。输出 JSON {"score":整数,"evidence":"回答中的原文片段"}。',
     "fixture": {"score": 2, "evidence": "固定查询"}},
    {"id": "report_suggestion", "prompt": '合成评估：回答提到固定查询，未说明指标。输出 JSON {"suggestions":["具体练习建议"]}，1至3条，不给招聘结论。',
     "fixture": {"suggestions": ["为固定查询标注相关材料，并说明比较使用的指标。"]}},
]
MAX_ATTEMPTS = 2
MAX_OUTPUT = 256


def validate(case_id: str, content: str, finish_reason: str = "stop") -> dict:
    """只校验能力试测形态，不把通过当作面试内容质量通过。"""
    if finish_reason != "stop":
        raise ValueError("INCOMPLETE_OUTPUT")
    if len(content.encode("utf-8")) > 16384:
        raise ValueError("OUTPUT_TOO_LARGE")
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result
    value = json.loads(content, object_pairs_hook=no_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("OBJECT_REQUIRED")
    expected = next((case["fixture"] for case in CASES if case["id"] == case_id), None)
    if expected is None or set(value) != set(expected):
        raise ValueError("SCHEMA_KEYS_INVALID")
    nonempty = lambda item: isinstance(item, str) and bool(item.strip())
    valid = False
    if case_id == "connection_json":
        valid = value["ok"] is True
    elif case_id == "extract_profile":
        valid = value["skills"] == ["Python"] and value["project"] == "课程检索练习"
    elif case_id == "rewrite_question":
        valid = nonempty(value["question"])
    elif case_id == "probe":
        valid = type(value["should_probe"]) is bool
        if value["should_probe"] is True:
            valid = valid and value["purpose"] in ("clarify", "deepen", "check_consistency") and nonempty(value["question"])
        else:
            valid = valid and value["purpose"] is None and value["question"] is None
    elif case_id == "score_evidence":
        valid = type(value["score"]) is int and 0 <= value["score"] <= 4 and nonempty(value["evidence"]) and value["evidence"] in ANSWER
    elif case_id == "report_suggestion":
        items = value["suggestions"]
        valid = isinstance(items, list) and 1 <= len(items) <= 3 and all(nonempty(x) for x in items)
    if not valid:
        raise ValueError("SCHEMA_VALUE_INVALID")
    return value


def build_plan() -> dict:
    rows = []
    total = Decimal("0")
    for case in CASES:
        messages = [{"role": "system", "content": "只处理合成样本，按要求输出一个 JSON 对象。"},
                    {"role": "user", "content": case["prompt"]}]
        # 不是供应商 tokenizer。按文本 UTF-8 字节数再加256包装余量规划，不能承诺账单绝对上限。
        allowance = sum(len(m["content"].encode("utf-8")) for m in messages) + 256
        request = {"model": PRICE["model"], "messages": messages, "max_tokens": MAX_OUTPUT,
                   "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}, "stream": False}
        attempt_cost = (Decimal(allowance) * Decimal(PRICE["input_cache_miss_per_million"]) +
                        Decimal(MAX_OUTPUT) * Decimal(PRICE["output_per_million"])) / Decimal(1000000)
        total += attempt_cost * MAX_ATTEMPTS
        rows.append({"case_id": case["id"], "request": request, "planning_input_tokens": allowance,
                     "max_attempts_including_repair": MAX_ATTEMPTS, "estimated_usd_per_attempt": str(attempt_cost)})
    return {"experiment": "EXP-002-preparation", "mode": "offline_only", "live_calls_executed": 0,
            "price_snapshot": PRICE, "cases": rows, "max_proposed_http_attempts": len(rows) * MAX_ATTEMPTS,
            "estimated_usd_all_attempts": str(total), "proposed_budget_usd_not_authorized": "0.02",
            "timeout_seconds_proposed": 30, "credential_check": "not_performed_by_offline_tool",
            "limits": ["estimate_not_invoice_guarantee", "no_actual_provider_compatibility_test",
                       "schema_checks_not_semantic_quality", "no_live_transport_or_billing_ledger_yet"]}


def self_check() -> dict:
    for case in CASES:
        validate(case["id"], json.dumps(case["fixture"], ensure_ascii=False))
    negatives = [
        ("connection_json", "[]", "stop"),
        ("connection_json", '{"ok":true,"ok":false}', "stop"),
        ("connection_json", "not json", "stop"),
        ("connection_json", '{"ok":true}', "length"),
        ("score_evidence", '{"score":true,"evidence":"固定查询"}', "stop"),
        ("score_evidence", '{"score":5,"evidence":"固定查询"}', "stop"),
        ("score_evidence", '{"score":2,"evidence":"我测出了90%"}', "stop"),
        ("probe", '{"should_probe":true,"purpose":"give_answer","question":"提示"}', "stop"),
        ("probe", '{"should_probe":false,"purpose":null,"question":"仍在提问"}', "stop"),
        ("report_suggestion", '{"suggestions":[]}', "stop"),
    ]
    rejected = 0
    for case_id, content, finish_reason in negatives:
        try:
            validate(case_id, content, finish_reason)
        except ValueError:
            rejected += 1
    if rejected != len(negatives):
        raise RuntimeError("negative_fixture_accepted")
    plan = build_plan()
    if Decimal(plan["estimated_usd_all_attempts"]) >= Decimal("0.02"):
        raise RuntimeError("proposal_does_not_fit_estimate")
    return {"positive_fixtures": len(CASES), "negative_fixtures_rejected": rejected,
            "live_calls_executed": 0, "all_passed": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"plan": build_plan(), "offline_validation": self_check()}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # 防止误覆盖已审阅的价格/样本快照。
        with args.output.open("x", encoding="utf-8") as output:
            output.write(text)
    print(text)


if __name__ == "__main__":
    main()
