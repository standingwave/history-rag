"""Helpers shared across sources."""
import re

# Text that likely contains a credential -> drop the chunk entirely, so it is
# never embedded or surfaced back into a session. Shared by every source that
# ingests free-form text (shell commands, URLs, notes).
#
# Two kinds of pattern: trigger words (a label like "password" near a value)
# and provider key SHAPES, for keys that appear with no label at all — a vault
# note reading "new relic: NRAK-..." has no trigger word, only the shape.
# Shapes must be precise enough that prose can't match: each is anchored at a
# word boundary and requires a long run of key-safe characters, so
# "desk-mounted-monitor-arm" or "public_html" stay clear.
SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|bearer|authorization|://[^/\s:]+:[^/\s@]+@"
    r"|AKIA[0-9A-Z]{16}"                    # AWS access key id
    r"|\bNRAK-[A-Z0-9]{20,}"                # New Relic
    r"|\b(public|private)_[A-Za-z0-9+/=]{16,}"   # ImageKit-style keypairs
    r"|\bsk-[A-Za-z0-9_-]{16,}"             # OpenAI / Anthropic / Stripe secret keys
    r"|\b(ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}|\bgithub_pat_"   # GitHub tokens
    r"|\bxox[baprs]-"                       # Slack tokens
    r"|\bAIza[0-9A-Za-z_-]{35}"             # Google API keys
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ"        # JWT (header.payload prefix)
    r")"
)
