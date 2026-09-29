from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import re
import unicodedata

from .config import SecurityConfig
from .errors import AccessDenied


class AccessController:
    def __init__(self, config: SecurityConfig) -> None:
        self.config = config

    def authorize(self, token: str | None, action: str) -> str:
        if not token:
            raise AccessDenied(f"Missing token for action '{action}'.")
        role = self._role_for_token(token)
        if role is None:
            raise AccessDenied(f"Unknown token for action '{action}'.")
        if action not in self.config.role_actions.get(role, ()):
            raise AccessDenied(f"Role '{role}' cannot perform action '{action}'.")
        return role

    def _role_for_token(self, token: str) -> str | None:
        for configured_token, role in self.config.tokens.items():
            if configured_token.startswith("sha256:"):
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if digest == configured_token.split(":", 1)[1]:
                    return role
            elif configured_token == token:
                return role
        return None


@dataclass
class PromptInjectionFinding:
    score: int
    patterns: list[str]

    @property
    def suspicious(self) -> bool:
        return self.score > 0


class PromptInjectionProtector:
    PATTERNS: tuple[tuple[str, int], ...] = (
        (r"\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions\b", 2),
        (r"\b(disregard|override)\s+(the\s+)?(system|developer|previous)\s+instructions\b", 2),
        (r"\breveal\s+(the\s+)?(system|developer)\s+prompt\b", 2),
        (r"\bprint\s+(the\s+)?(system|developer)\s+prompt\b", 2),
        (r"\bexfiltrat(e|ion)\b", 2),
        (r"\bdisable\s+(safety|guardrails?|filters?)\b", 2),
        (r"\byou\s+are\s+now\s+(in|a)\s+", 1),
        (r"\bjailbreak\b", 1),
        (r"<\s*script\b", 2),
        (r"\btool\s+call\s+.*\b(secret|token|password)\b", 2),
        (r"\b(?:do not|don't|never|omit|skip)\s+(?:provide\s+|include\s+)?(?:citations?|sources?|grounding)\b", 2),
        (r"\b(?:system|developer|assistant)\s*(?:message|instruction|override)?\s*:\s*(?:ignore|always|you|override|reveal|answer)\b", 2),
        (r"\b(?:ignore|bypass|override)\s+(?:the\s+|all\s+)?(?:access|authori[sz]ation|security|policy)\s*(?:controls?|checks?|rules?|filters?)\b", 2),
        (r"\b(?:send|upload|transmit)\s+(?:all\s+|the\s+)?(?:secrets?|tokens?|credentials?|passwords?)\s+to\b", 2),
        (r"<\s*(?:system|developer)\s*>|\[INST\]|<\|im_start\|>", 2),
        (r"\bforget\s+(?:all\s+|your\s+)?(?:prior\s+|previous\s+)?instructions\b", 2),
    )

    def scan(self, text: str) -> PromptInjectionFinding:
        text = unicodedata.normalize("NFKC", html.unescape(text))
        text = "".join(char for char in text if unicodedata.category(char) != "Cf")
        matched: list[str] = []
        score = 0
        for pattern, weight in self.PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                matched.append(pattern)
                score += weight
        return PromptInjectionFinding(score=score, patterns=matched)


class SensitiveDataMasker:
    EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)")
    SECRET = re.compile(
        r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[A-Za-z0-9_\-./+=]{12,}\b",
        flags=re.IGNORECASE,
    )
    CREDIT_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

    def mask(self, text: str) -> tuple[str, bool]:
        masked = text
        masked = self.SECRET.sub("[SECRET]", masked)
        masked = self.EMAIL.sub("[EMAIL]", masked)
        masked = self.SSN.sub("[SSN]", masked)
        masked = self.PHONE.sub("[PHONE]", masked)
        masked = self.CREDIT_CARD.sub(self._mask_card_candidate, masked)
        return masked, masked != text

    def _mask_card_candidate(self, match: re.Match[str]) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        if 13 <= len(digits) <= 19 and self._luhn(digits):
            return "[CREDIT_CARD]"
        return candidate

    def _luhn(self, digits: str) -> bool:
        total = 0
        reverse_digits = digits[::-1]
        for index, char in enumerate(reverse_digits):
            value = int(char)
            if index % 2 == 1:
                value *= 2
                if value > 9:
                    value -= 9
            total += value
        return total % 10 == 0
