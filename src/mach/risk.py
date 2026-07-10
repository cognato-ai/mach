"""Local risk evaluation for agent steps.

Facts are always captured; this module annotates steps with structured
``risk_flags``. Enterprise/backend rules can add more flags later — local
evaluation is deterministic, config-driven, and merge-friendly.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Optional

Severity = Literal["none", "low", "medium", "high", "critical"]
RiskCategory = Literal["path", "content", "command", "tool", "custom"]
RiskSource = Literal["local", "external"]

_SEVERITY_RANK = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


@dataclass
class RiskFlag:
    rule_id: str
    severity: Severity
    explanation: str
    resolved: bool = False
    source: RiskSource = "local"
    category: RiskCategory = "custom"
    target: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.target is None:
            d.pop("target", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskFlag":
        return cls(
            rule_id=str(data.get("rule_id") or "UNKNOWN"),
            severity=_normalize_severity(data.get("severity")),
            explanation=str(data.get("explanation") or ""),
            resolved=bool(data.get("resolved", False)),
            source="external" if data.get("source") == "external" else "local",
            category=_normalize_category(data.get("category")),
            target=data.get("target"),
        )


@dataclass
class _PatternRule:
    rule_id: str
    severity: Severity
    patterns: list[re.Pattern[str]]
    explanation: str
    category: RiskCategory
    # What field(s) the rule inspects
    target_kind: Literal["path", "content", "command", "tool_name"]


def _compile_patterns(patterns: Iterable[str], *, flags: int = re.IGNORECASE) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, flags))
        except re.error:
            continue
    return compiled


def _normalize_severity(value: Any) -> Severity:
    text = str(value or "none").lower()
    if text in _SEVERITY_RANK:
        return text  # type: ignore[return-value]
    return "none"


def _normalize_category(value: Any) -> RiskCategory:
    text = str(value or "custom").lower()
    if text in {"path", "content", "command", "tool", "custom"}:
        return text  # type: ignore[return-value]
    return "custom"


def severity_rank(value: str) -> int:
    return _SEVERITY_RANK.get(str(value or "none").lower(), 0)


def max_severity(values: Iterable[str]) -> Severity:
    best: Severity = "none"
    for value in values:
        candidate = _normalize_severity(value)
        if severity_rank(candidate) > severity_rank(best):
            best = candidate
    return best


# ── Built-in rule definitions ────────────────────────────────────────────────

_PATH_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "PATH_ENV_SECRETS",
        "severity": "high",
        "patterns": [
            r"(^|/)\.env(\.|$)",
            r"(^|/)\.env\.[^/]+$",
            r"(^|/)secrets?(/|$)",
            r"(^|/)credentials?\.(json|ya?ml|toml|env)$",
        ],
        "explanation": "Change touches environment or credentials material.",
    },
    {
        "rule_id": "PATH_AUTH_CRYPTO",
        "severity": "high",
        "patterns": [
            r"(^|/)(auth|oauth|jwt|session)s?(/|$)",
            r"\.(pem|key|p12|pfx|jks)$",
            r"(^|/)id_rsa",
            r"(^|/)\.ssh(/|$)",
        ],
        "explanation": "Change touches authentication or cryptographic key material.",
    },
    {
        "rule_id": "PATH_INFRA",
        "severity": "medium",
        "patterns": [
            r"(^|/)(terraform|pulumi|cloudformation)(/|$)",
            r"(^|/)Dockerfile",
            r"(^|/)docker-compose\.(ya?ml)$",
            r"(^|/)\.github/workflows/",
            r"(^|/)(k8s|kubernetes|helm)(/|$)",
        ],
        "explanation": "Change touches infrastructure or deployment automation.",
    },
    {
        "rule_id": "PATH_PII_FINANCIAL",
        "severity": "high",
        "patterns": [
            r"(^|/)(pii|ssn|passport)(/|$)",
            r"(^|/)(payment|billing|invoice|ledger)s?(/|$)",
        ],
        "explanation": "Change may involve PII or financial data paths.",
    },
]

_CONTENT_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "SECRET_PRIVATE_KEY",
        "severity": "critical",
        "patterns": [
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        ],
        "explanation": "Content appears to contain a private key block.",
    },
    {
        "rule_id": "SECRET_AWS_KEY",
        "severity": "critical",
        "patterns": [
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bASIA[0-9A-Z]{16}\b",
        ],
        "explanation": "Content appears to contain an AWS access key id.",
    },
    {
        "rule_id": "SECRET_GITHUB_TOKEN",
        "severity": "critical",
        "patterns": [
            r"\bghp_[A-Za-z0-9]{20,}\b",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
            r"\bgho_[A-Za-z0-9]{20,}\b",
        ],
        "explanation": "Content appears to contain a GitHub token.",
    },
    {
        "rule_id": "SECRET_GENERIC_TOKEN",
        "severity": "high",
        "patterns": [
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]",
            r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*",
        ],
        "explanation": "Content appears to embed an API key or bearer token.",
    },
]

_COMMAND_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "CMD_DESTRUCTIVE_FS",
        "severity": "critical",
        "patterns": [
            r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+[/~]",
            r"\bmkfs\b",
            r"\bdd\s+if=",
            r">\s*/dev/sd",
        ],
        "explanation": "Command looks like destructive filesystem activity.",
    },
    {
        "rule_id": "CMD_REMOTE_CODE_EXEC",
        "severity": "critical",
        "patterns": [
            r"\bcurl\b[^|\n]*\|\s*(ba)?sh\b",
            r"\bwget\b[^|\n]*\|\s*(ba)?sh\b",
            r"\bcurl\b[^;\n]*\|\s*python\b",
        ],
        "explanation": "Command pipes remote content into a shell interpreter.",
    },
    {
        "rule_id": "CMD_FORCE_PUSH",
        "severity": "high",
        "patterns": [
            r"\bgit\s+push\b[^\n]*--force\b",
            r"\bgit\s+push\b[^\n]*\s-f\b",
            r"\bgit\s+reset\s+--hard\b",
        ],
        "explanation": "Command rewrites git history or force-pushes.",
    },
    {
        "rule_id": "CMD_PRIVILEGE_WIDE_PERMS",
        "severity": "medium",
        "patterns": [
            r"\bchmod\s+(-R\s+)?777\b",
            r"\bchown\s+-R\s+root\b",
            r"\bsudo\s+su\b",
        ],
        "explanation": "Command widens privileges or ownership aggressively.",
    },
    {
        "rule_id": "CMD_DB_DESTRUCTIVE",
        "severity": "high",
        "patterns": [
            r"(?i)\bDROP\s+(TABLE|DATABASE|SCHEMA)\b",
            r"(?i)\bTRUNCATE\s+TABLE\b",
            r"(?i)\bDELETE\s+FROM\b[^\n]*\bWHERE\s+1\s*=\s*1\b",
        ],
        "explanation": "Command or SQL looks like destructive database activity.",
    },
]

_TOOL_NAME_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "TOOL_SHELL_EXEC",
        "severity": "low",
        "patterns": [
            r"^(Bash|Shell|shell|terminal|run_terminal_command|execute)$",
        ],
        "explanation": "Agent invoked a shell/execution tool.",
    },
]


def _build_rules(definitions: list[dict[str, Any]], category: RiskCategory, target_kind: str) -> list[_PatternRule]:
    rules: list[_PatternRule] = []
    for item in definitions:
        patterns = _compile_patterns(item.get("patterns") or [])
        if not patterns:
            continue
        rules.append(
            _PatternRule(
                rule_id=str(item["rule_id"]),
                severity=_normalize_severity(item.get("severity")),
                patterns=patterns,
                explanation=str(item.get("explanation") or item["rule_id"]),
                category=category,
                target_kind=target_kind,  # type: ignore[arg-type]
            )
        )
    return rules


def _extra_rules_from_config(
    extras: list[dict[str, Any]] | None,
    *,
    category: RiskCategory,
    target_kind: str,
    default_prefix: str,
) -> list[_PatternRule]:
    rules: list[_PatternRule] = []
    if not extras:
        return rules
    for index, item in enumerate(extras):
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern") or item.get("regex")
        patterns = item.get("patterns")
        raw_patterns: list[str] = []
        if pattern:
            raw_patterns.append(str(pattern))
        if isinstance(patterns, list):
            raw_patterns.extend(str(p) for p in patterns)
        compiled = _compile_patterns(raw_patterns)
        if not compiled:
            continue
        rule_id = str(item.get("rule_id") or f"{default_prefix}_{index + 1}")
        rules.append(
            _PatternRule(
                rule_id=rule_id,
                severity=_normalize_severity(item.get("severity", "medium")),
                patterns=compiled,
                explanation=str(item.get("explanation") or f"Custom {category} rule matched."),
                category=category,
                target_kind=target_kind,  # type: ignore[arg-type]
            )
        )
    return rules


@dataclass
class RiskEngine:
    """Evaluates structured risk flags for a step payload."""

    enabled: bool = True
    disabled_rules: set[str] = field(default_factory=set)
    path_rules: list[_PatternRule] = field(default_factory=list)
    content_rules: list[_PatternRule] = field(default_factory=list)
    command_rules: list[_PatternRule] = field(default_factory=list)
    tool_rules: list[_PatternRule] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> "RiskEngine":
        config = config or {}
        risk_cfg = config.get("risk") if isinstance(config.get("risk"), dict) else {}
        enabled = bool(risk_cfg.get("enabled", True)) if risk_cfg else bool(config.get("risk_enabled", True))
        disabled = {
            str(item)
            for item in (risk_cfg.get("disabled_rules") or config.get("risk_disabled_rules") or [])
            if item
        }

        path_rules = _build_rules(_PATH_RULES, "path", "path")
        path_rules.extend(
            _extra_rules_from_config(
                risk_cfg.get("extra_path_patterns"),
                category="path",
                target_kind="path",
                default_prefix="CUSTOM_PATH",
            )
        )

        content_rules = _build_rules(_CONTENT_RULES, "content", "content")
        content_rules.extend(
            _extra_rules_from_config(
                risk_cfg.get("extra_content_patterns"),
                category="content",
                target_kind="content",
                default_prefix="CUSTOM_CONTENT",
            )
        )

        command_rules = _build_rules(_COMMAND_RULES, "command", "command")
        command_rules.extend(
            _extra_rules_from_config(
                risk_cfg.get("extra_command_patterns"),
                category="command",
                target_kind="command",
                default_prefix="CUSTOM_CMD",
            )
        )

        tool_rules = _build_rules(_TOOL_NAME_RULES, "tool", "tool_name")

        return cls(
            enabled=enabled,
            disabled_rules=disabled,
            path_rules=path_rules,
            content_rules=content_rules,
            command_rules=command_rules,
            tool_rules=tool_rules,
        )

    def evaluate_step(
        self,
        step: dict[str, Any],
        *,
        content_text: str | None = None,
    ) -> tuple[list[dict[str, Any]], Severity]:
        """Return merged flags and aggregate severity for a step dict.

        Existing flags on the step (e.g. enterprise annotations) are preserved.
        Local engine flags are added when they are not already present for the
        same ``rule_id`` + ``target``.
        """
        existing = [RiskFlag.from_dict(flag) for flag in (step.get("risk_flags") or []) if isinstance(flag, dict)]
        # Treat caller-supplied flags without source as external so we never drop them.
        for flag in existing:
            if flag.source != "local":
                flag.source = "external"

        local: list[RiskFlag] = []
        if self.enabled:
            local.extend(self._eval_paths(step.get("file_changes") or []))
            tool = step.get("tool") if isinstance(step.get("tool"), dict) else {}
            tool_name = str(tool.get("name") or "")
            tool_content = str(tool.get("content") or "")
            body = content_text if content_text is not None else str(step.get("content") or "")
            # Prefer tool payload for tool steps when available.
            scan_text = tool_content or body
            local.extend(self._eval_content(scan_text))
            local.extend(self._eval_commands(scan_text))
            local.extend(self._eval_tool_name(tool_name))

        merged = self._merge_flags(existing, local)
        level = max_severity(
            [flag.severity for flag in merged]
            + [_normalize_severity(step.get("risk_level"))]
        )
        return [flag.to_dict() for flag in merged], level

    def _rule_enabled(self, rule_id: str) -> bool:
        return rule_id not in self.disabled_rules

    def _match_rules(
        self,
        rules: list[_PatternRule],
        text: str,
        *,
        target_prefix: str,
    ) -> list[RiskFlag]:
        if not text:
            return []
        flags: list[RiskFlag] = []
        for rule in rules:
            if not self._rule_enabled(rule.rule_id):
                continue
            for pattern in rule.patterns:
                match = pattern.search(text)
                if not match:
                    continue
                snippet = match.group(0)
                target = f"{target_prefix}:{snippet[:120]}"
                flags.append(
                    RiskFlag(
                        rule_id=rule.rule_id,
                        severity=rule.severity,
                        explanation=rule.explanation,
                        category=rule.category,
                        source="local",
                        target=target,
                    )
                )
                break  # one hit per rule per text unit is enough
        return flags

    def _eval_paths(self, file_changes: list[Any]) -> list[RiskFlag]:
        flags: list[RiskFlag] = []
        seen_paths: set[str] = set()
        for change in file_changes:
            if not isinstance(change, dict):
                continue
            path = str(change.get("file_path") or "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            for rule in self.path_rules:
                if not self._rule_enabled(rule.rule_id):
                    continue
                if any(pattern.search(path) for pattern in rule.patterns):
                    flags.append(
                        RiskFlag(
                            rule_id=rule.rule_id,
                            severity=rule.severity,
                            explanation=f"{rule.explanation} ({path})",
                            category="path",
                            source="local",
                            target=f"path:{path}",
                        )
                    )
        return flags

    def _eval_content(self, text: str) -> list[RiskFlag]:
        return self._match_rules(self.content_rules, text, target_prefix="content")

    def _eval_commands(self, text: str) -> list[RiskFlag]:
        return self._match_rules(self.command_rules, text, target_prefix="command")

    def _eval_tool_name(self, tool_name: str) -> list[RiskFlag]:
        if not tool_name:
            return []
        flags: list[RiskFlag] = []
        for rule in self.tool_rules:
            if not self._rule_enabled(rule.rule_id):
                continue
            if any(pattern.search(tool_name) for pattern in rule.patterns):
                flags.append(
                    RiskFlag(
                        rule_id=rule.rule_id,
                        severity=rule.severity,
                        explanation=f"{rule.explanation} (tool={tool_name})",
                        category="tool",
                        source="local",
                        target=f"tool:{tool_name}",
                    )
                )
        return flags

    @staticmethod
    def _merge_flags(existing: list[RiskFlag], local: list[RiskFlag]) -> list[RiskFlag]:
        merged: list[RiskFlag] = []
        seen: set[tuple[str, str]] = set()

        def key(flag: RiskFlag) -> tuple[str, str]:
            return (flag.rule_id, flag.target or "")

        for flag in existing:
            k = key(flag)
            if k in seen:
                continue
            seen.add(k)
            merged.append(flag)
        for flag in local:
            k = key(flag)
            if k in seen:
                continue
            seen.add(k)
            merged.append(flag)
        return merged


def evaluate_step_risk(
    step: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    content_text: str | None = None,
) -> tuple[list[dict[str, Any]], Severity]:
    """Convenience wrapper used by SessionStore."""
    return RiskEngine.from_config(config).evaluate_step(step, content_text=content_text)
