"""File-only classification rules, independent of the AI recommendation prompts."""
from ..ai import fallback, redact
from ..departments import CATALOG
from ..rule_classifier import classify_rules

FILE_KEYWORDS = {department["name"]: department["keywords"] for department in CATALOG}


def file_fallback(title, content, reason="파일 분류 규칙으로 1차 분류했습니다."):
    record = fallback(title, content, reason)
    decision = classify_rules(f"{redact(title)} {record['content']}", FILE_KEYWORDS)
    uncertain = decision["requires_llm"] or decision["top_score"] == 0
    record.update(
        category=decision["category"], confidence=decision["confidence"],
        rule_decision=decision, processing_mode="rule", llm_attempted=False,
        keywords=[hit["keyword"] for hit in decision["matched"][decision["category"]]][:5],
        needs_review=record["needs_review"] or uncertain,
        review_reason="분류 근거가 부족하거나 부서가 모호하여 검토가 필요합니다." if uncertain else record["review_reason"],
    )
    return record
