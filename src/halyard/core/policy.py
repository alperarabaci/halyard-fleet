"""Deciding how dangerous a tool call is.

Risk is assigned here and nowhere else. An agent may say a command is harmless,
and that claim is worth something — but it comes from the same reasoning that
produced the command, so it cannot also be what checks it. `classify()` accepts
a declared level and lets it *raise* the result, never lower it.

Two properties matter more than the rule list itself:

**The highest match wins.** A shell command is not one action. `git status &&
rm -rf build` matches a read-only rule and a destructive one, and anything that
stops at the first match would put it on a card labelled low risk. Every rule is
evaluated and the worst outcome is the answer.

**An unrecognised command is medium, not low.** The rules cannot enumerate
everything, and the cost of guessing wrong in the two directions is not
symmetric: an over-cautious label wastes a moment of the approver's attention,
while an under-cautious one is how something destructive gets waved through.

Rules are data, so extending them is a line in a list. They are matched against
the *redacted* command, which means policy never sees a secret — and also means
rules must not depend on the shape of a value that has been masked away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from halyard.core.events import RiskLevel

#: Ordering used to take the worst of several matches.
_SEVERITY: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}


@dataclass(frozen=True)
class PolicyRule:
    """One pattern and the risk it implies."""

    name: str
    risk: RiskLevel
    pattern: re.Pattern[str]


def _rule(name: str, risk: RiskLevel, pattern: str) -> PolicyRule:
    return PolicyRule(name=name, risk=risk, pattern=re.compile(pattern, re.IGNORECASE))


DEFAULT_RULES: tuple[PolicyRule, ...] = (
    # --- low: reads, checks, and things that only report -------------------
    _rule("git_read", RiskLevel.LOW, r"\bgit\s+(status|diff|log|show|branch|remote|blame)\b"),
    _rule(
        "shell_read",
        RiskLevel.LOW,
        r"^\s*(ls|pwd|cat|head|tail|wc|file|stat|which|whoami|date|echo|grep|rg|fd|find)\b",
    ),
    _rule(
        "test_run",
        RiskLevel.LOW,
        r"\b(pytest|tox|nox|jest|vitest|(npm|yarn|pnpm)\s+(run\s+)?test|go\s+test"
        r"|cargo\s+test|mvn\s+test|gradlew?\s+test|rspec|phpunit)\b",
    ),
    _rule(
        "lint_or_typecheck",
        RiskLevel.LOW,
        r"\b(ruff|flake8|pylint|mypy|pyright|eslint|prettier|black|gofmt|clippy)\b",
    ),
    _rule(
        "inspect_infrastructure",
        RiskLevel.LOW,
        r"\b(docker\s+(ps|logs|images|inspect)|kubectl\s+(get|describe|logs|top)"
        r"|terraform\s+(plan|show|validate)|helm\s+(list|status|get))\b",
    ),
    # --- medium: changes this machine, reversibly ---------------------------
    _rule("git_write", RiskLevel.MEDIUM, r"\bgit\s+(add|commit|checkout|switch|merge|stash|tag)\b"),
    _rule("git_push", RiskLevel.MEDIUM, r"\bgit\s+push\b"),
    _rule(
        "package_install",
        RiskLevel.MEDIUM,
        r"\b(pip|pip3|uv|poetry|npm|yarn|pnpm|cargo|go|brew|apt|apt-get)\s+"
        r"(install|add|sync|get)\b",
    ),
    _rule("file_write", RiskLevel.MEDIUM, r"(>>?\s*\S|\b(tee|mv|cp|touch|mkdir|ln)\b)"),
    _rule(
        "container_lifecycle",
        RiskLevel.MEDIUM,
        r"\b(docker(\s+compose)?\s+(up|start|stop|restart|build)|docker\s+run)\b",
    ),
    _rule("local_migration", RiskLevel.MEDIUM, r"\b(alembic|flyway|liquibase|migrate)\b"),
    # --- high: destroys, deploys, or reaches for a credential ---------------
    _rule("recursive_delete", RiskLevel.HIGH, r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*\s|-[a-zA-Z]*f)"),
    _rule("force_push", RiskLevel.HIGH, r"\bgit\s+push\b.*(--force|--force-with-lease|\s-f\b)"),
    _rule("history_rewrite", RiskLevel.HIGH, r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*[dfx])"),
    _rule(
        "container_teardown",
        RiskLevel.HIGH,
        r"\b(docker(\s+compose)?\s+down|docker\s+(rm|rmi|volume\s+rm|system\s+prune))\b",
    ),
    _rule(
        "destructive_sql",
        RiskLevel.HIGH,
        r"\b(drop\s+(table|database|schema|index)|truncate\s+table|delete\s+from)\b",
    ),
    _rule(
        "infrastructure_change",
        RiskLevel.HIGH,
        r"\b(terraform\s+(apply|destroy)|kubectl\s+(delete|apply|scale|rollout)"
        r"|helm\s+(upgrade|install|uninstall|rollback)|pulumi\s+up)\b",
    ),
    _rule("deploy", RiskLevel.HIGH, r"\b(deploy|release|publish)\b"),
    _rule(
        "cloud_cli",
        RiskLevel.HIGH,
        r"^\s*(aws|gcloud|az|doctl|flyctl|heroku|vercel|wrangler)\b",
    ),
    _rule(
        "secret_access",
        RiskLevel.HIGH,
        r"(\b(printenv|env)\b\s*($|\|)|\bcat\b[^|;]*(\.env|credentials|id_rsa|\.pem|\.netrc)"
        r"|\b(security\s+find-generic-password|gpg\s+--decrypt)\b)",
    ),
    _rule("privilege_escalation", RiskLevel.HIGH, r"^\s*sudo\b|\bchmod\s+(-R\s+)?777\b"),
    # Downloading something and executing it unread. Anything here is arbitrary
    # code from the network, whatever the URL looks like.
    _rule(
        "pipe_to_shell",
        RiskLevel.HIGH,
        r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|)sh\b",
    ),
)

#: What an unmatched command is worth. See the module docstring.
DEFAULT_RISK = RiskLevel.MEDIUM


@dataclass(frozen=True)
class PolicyDecision:
    """The risk assigned to a call, and how it was reached."""

    risk: RiskLevel
    #: Every rule that matched, in rule order. Recorded so a surprising label
    #: can be explained without re-running the classifier by hand.
    matched: tuple[str, ...]
    #: True when nothing matched and `DEFAULT_RISK` was used.
    defaulted: bool
    #: Set when the agent's own claim was more alarming than the rules, and was
    #: therefore taken instead.
    escalated_by_agent: bool = False


class Policy:
    """Classifies commands by risk. Stateless and safe to share."""

    def __init__(
        self,
        rules: tuple[PolicyRule, ...] = DEFAULT_RULES,
        *,
        default_risk: RiskLevel = DEFAULT_RISK,
    ) -> None:
        self._rules = rules
        self._default_risk = default_risk

    def classify(self, command: str, *, declared: RiskLevel | None = None) -> PolicyDecision:
        """Assign a risk level to an already-redacted command.

        `declared` is whatever the agent said about its own call. It can raise
        the result and cannot lower it: an agent warning us about itself is
        information worth keeping, while an agent reassuring us about itself is
        the thing this whole system exists to not rely on.
        """
        matched = tuple(rule.name for rule in self._rules if rule.pattern.search(command))
        risks = [rule.risk for rule in self._rules if rule.name in matched]

        if risks:
            risk = max(risks, key=lambda level: _SEVERITY[level])
            defaulted = False
        else:
            risk = self._default_risk
            defaulted = True

        escalated = False
        if declared is not None and _SEVERITY[declared] > _SEVERITY[risk]:
            risk = declared
            escalated = True

        return PolicyDecision(
            risk=risk,
            matched=matched,
            defaulted=defaulted,
            escalated_by_agent=escalated,
        )
