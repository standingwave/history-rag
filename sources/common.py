"""Helpers shared across sources."""
import re

# Text that likely contains a credential -> drop the chunk entirely, so it is
# never embedded or surfaced back into a session. Shared by every source that
# ingests free-form text (shell commands, URLs, notes).
SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|bearer|authorization|AKIA[0-9A-Z]{16}|://[^/\s:]+:[^/\s@]+@)"
)
