"""服务端敏感信息脱敏：日志详情返回前，把命令/输出里的密钥、token、连接串等抹掉。

「日志详情」会暴露 Agent 执行的原始命令与工具输出（含 Bash 全文），可能带凭证。
返回给前端前统一过一遍；前端还有一层兜底脱敏（防御纵深）。
内置脱敏规则集。
"""
import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # AWS access key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    # AWS secret access key
    (re.compile(r"(?:aws_secret_access_key|secret_?access_?key)\s*[=:]\s*[A-Za-z0-9/+=]{40}", re.I),
     "[REDACTED AWS SECRET]"),
    # PEM 私钥
    (re.compile(r"-----BEGIN[A-Z\s]*PRIVATE KEY-----[\s\S]*?-----END[A-Z\s]*PRIVATE KEY-----"),
     "[REDACTED PRIVATE KEY]"),
    # GitHub token
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,255}\b"), "[REDACTED GITHUB TOKEN]"),
    # GitLab PAT
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "[REDACTED GITLAB TOKEN]"),
    # OpenAI / Anthropic API key
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED API KEY]"),
    # Slack token
    (re.compile(r"\bxox[bporas]-[A-Za-z0-9-]{10,}\b"), "[REDACTED SLACK TOKEN]"),
    # JWT
    (re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED JWT]"),
    # Bearer token
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.I), "Bearer [REDACTED]"),
    # 带密码的连接串
    (re.compile(r"(?:postgres|mysql|mongodb|redis|amqp)(?:ql)?://[^:\s]+:[^@\s]+@", re.I),
     "[REDACTED CONNECTION STRING]@"),
    # 通用 key=value 密钥环境变量
    (re.compile(r"(?:API_KEY|API_SECRET|SECRET_KEY|SECRET|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY|"
                r"DATABASE_URL|DB_PASSWORD|DB_URL|REDIS_URL|PASSWORD|TOKEN)\s*[=:]\s*\S+", re.I),
     "[REDACTED CREDENTIAL]"),
]


def redact_secrets(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
