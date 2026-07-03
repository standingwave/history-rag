"""Secret redaction — highest stakes in the repo: a false negative embeds a
credential and can surface it back into a session."""
from sources.common import SECRET_RE
from sources.shell import _FLAG_SECRET_RE, _keep

# Must be caught by the SHARED regex (applies to shell, browser, obsidian).
SHARED_MUST_DROP = [
    "export API_KEY=sk-abc123def456",
    "curl -H 'Authorization: Bearer eyJhbGciOi'",
    "echo password=hunter2 >> creds",
    "aws s3 ls --access-key AKIAIOSFODNN7EXAMPLE",
    "git clone https://user:hunter2@github.com/x/y.git",
    "PASSWD=root mysql",
    "set PRIVATE_KEY /tmp/k",
]

# Must survive the shared regex (browser URLs / notes must not over-drop).
SHARED_MUST_KEEP = [
    "https://example.com/my-project-x/readme",       # -p + 6 chars is fine here
    "https://mobalytics.gg/poe-2/builds",
    "brew install ollama",
    "git log --oneline",
]

# Shell-only pattern (mysql -pSecret style); deliberately NOT shared because
# it would drop URLs/paths like /my-project-x.
FLAG_MUST_DROP = ["mysql -uroot -pXk29vLmQ4"]   # no shared trigger words
FLAG_MUST_KEEP = ["ls -p", "grep -P 'x'"]

def test_shared_regex_drops_credentials():
    for s in SHARED_MUST_DROP:
        assert SECRET_RE.search(s), f"should drop: {s}"

def test_shared_regex_keeps_innocent_text():
    for s in SHARED_MUST_KEEP:
        assert not SECRET_RE.search(s), f"should keep: {s}"

def test_flag_pattern_is_shell_only():
    for s in FLAG_MUST_DROP:
        assert _FLAG_SECRET_RE.search(s), f"shell should drop: {s}"
        assert not SECRET_RE.search(s), f"shared regex must NOT own this: {s}"
    for s in FLAG_MUST_KEEP:
        assert not _FLAG_SECRET_RE.search(s), f"shell should keep: {s}"

def test_keep_applies_both_plus_stopwords_and_length():
    assert not _keep("ls")                            # stopword
    assert not _keep("cd")                            # too short + stopword
    assert not _keep("mysql -uroot -pSuperSecret1")   # flag secret
    assert not _keep("export API_KEY=sk-abc123")      # shared secret
    assert _keep("git log --oneline")
