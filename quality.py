from __future__ import annotations

from statistics import mean
from datetime import datetime

from .config import QualityConfig, RagConfig
from .models import CaseScore, DocumentRecord, EvaluationCase, QualityReport
from .retrieval import tokenize
from .rag import KnowledgeAssistant
from .knowledge_metadata import with_policy_history


class QualityGate:
    def __init__(self, config: QualityConfig, rag_config: RagConfig | None = None) -> None:
        self.config = config
        self.rag_config = rag_config or RagConfig()

    def evaluate(
        self,
        version_id: str,
        candidate_documents: list[DocumentRecord],
        eval_cases: list[EvaluationCase],
        baseline_documents: list[DocumentRecord] | None = None,
        now: datetime | None = None,
        historical_documents: list[DocumentRecord] | None = None,
    ) -> QualityReport:
        if not eval_cases:
            passed = not self.config.require_eval_cases
            reason = "" if passed else "no_evaluation_cases"
            return QualityReport(
                version_id=version_id,
                passed=passed,
                accuracy=1.0 if passed else 0.0,
                grounding=1.0 if passed else 0.0,
                rejected_reason=reason,
            )

        history = historical_documents or []
        candidate = self._score_documents(with_policy_history(candidate_documents, history), eval_cases, now)
        baseline = self._score_documents(with_policy_history(baseline_documents or [], history), eval_cases, now) if baseline_documents else None
        accuracy = mean(score.accuracy for score in candidate) if candidate else 0.0
        grounding = mean(score.grounding for score in candidate) if candidate else 0.0
        baseline_accuracy = mean(score.accuracy for score in baseline) if baseline else None
        baseline_grounding = mean(score.grounding for score in baseline) if baseline else None

        reasons: list[str] = []
        if accuracy < self.config.min_accuracy:
            reasons.append(f"accuracy {accuracy:.3f} below minimum {self.config.min_accuracy:.3f}")
        if grounding < self.config.min_grounding:
            reasons.append(f"grounding {grounding:.3f} below minimum {self.config.min_grounding:.3f}")
        if baseline_accuracy is not None and accuracy < baseline_accuracy - self.config.max_accuracy_regression:
            reasons.append(
                f"accuracy regression from {baseline_accuracy:.3f} to {accuracy:.3f}"
            )
        if baseline_grounding is not None and grounding < baseline_grounding - self.config.max_grounding_regression:
            reasons.append(
                f"grounding regression from {baseline_grounding:.3f} to {grounding:.3f}"
            )
        return QualityReport(
            version_id=version_id,
            passed=not reasons,
            accuracy=round(accuracy, 4),
            grounding=round(grounding, 4),
            baseline_accuracy=round(baseline_accuracy, 4) if baseline_accuracy is not None else None,
            baseline_grounding=round(baseline_grounding, 4) if baseline_grounding is not None else None,
            rejected_reason="; ".join(reasons),
            case_scores=candidate,
        )

    def _score_documents(self, documents: list[DocumentRecord], eval_cases: list[EvaluationCase], now=None) -> list[CaseScore]:
        assistant = KnowledgeAssistant(self.rag_config)
        scores: list[CaseScore] = []
        for case in eval_cases:
            result = assistant.answer(documents, case.question, case.role, None, product=case.product, region=case.region,
                                      as_of=case.as_of, now=now, top_k=self.config.top_k)
            contexts = result.contexts
            haystack = result.answer.lower() if result.status == "answered" else ""
            haystack_terms = set(tokenize(haystack))
            retrieved_paths = [context.path for context in contexts]
            required = [term.lower() for term in case.required_terms]
            forbidden = [term.lower() for term in case.forbidden_terms]
            missing = [term for term in required if not term_present(term, haystack, haystack_terms)]
            forbidden_found = [term for term in forbidden if term and term_present(term, haystack, haystack_terms)]
            if required:
                accuracy = (len(required) - len(missing)) / len(required)
            else:
                accuracy = 1.0 if contexts else 0.0
            source_hint = case.source_hint.lower().strip()
            source_match = True
            if source_hint:
                source_match = source_hint in haystack or any(source_hint in path.lower() for path in retrieved_paths)
            grounding = accuracy
            if source_hint:
                grounding = (accuracy * 0.75) + (0.25 if source_match else 0.0)
            if forbidden_found:
                accuracy = 0.0
                grounding = 0.0
            if case.expected_status != "answered":
                accuracy = grounding = float(result.status == case.expected_status and not result.contexts and not result.citations)
            elif result.status != "answered" or not result.citations:
                accuracy = grounding = 0.0
            scores.append(
                CaseScore(
                    question=case.question,
                    accuracy=round(accuracy, 4),
                    grounding=round(grounding, 4),
                    confidence=result.confidence,
                    retrieved_paths=retrieved_paths,
                    missing_terms=missing,
                    forbidden_terms_found=forbidden_found,
                )
            )
        return scores


def term_present(term: str, haystack: str, haystack_terms: set[str]) -> bool:
    if term in haystack:
        return True
    normalized_terms = tokenize(term)
    return bool(normalized_terms) and all(item in haystack_terms for item in normalized_terms)
