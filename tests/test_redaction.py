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
    # unlabeled key SHAPES (the 2025-07-20 vault-note incident: no trigger
    # word anywhere near the value)
    "new relic: NRAK-IOYR664U2IA93US35GIR1AJF530",
    "imagekit public_SYvfrf+on6GdDHFtR4KXHvln8kk=",
    "private_bQx91LmNe4Prv77Zw2yTk3s=",
    "sk-ant-api03-Zz9Yx8Ww7Vv6Uu5T",
    "remote add x ghp_16C7e42F292c6912E7710c838347Ae178B4a",
    "github_pat_11ABCDEFG",
    "slack hook xoxb-1234-abcd",
    "maps key AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
]

# Must survive the shared regex (browser URLs / notes must not over-drop).
SHARED_MUST_KEEP = [
    "https://example.com/my-project-x/readme",       # -p + 6 chars is fine here
    "https://mobalytics.gg/poe-2/builds",
    "brew install ollama",
    "git log --oneline",
    # near-misses for the shape patterns: boundaries and short runs
    "desk-mounted-monitor-arm-comparison-notes",     # \b keeps sk- out of prose
    "chmod 755 public_html && ls private_docs",      # short runs after the _
    "the private_messages setting toggles DMs",
    "NRAK notes from the retro",                     # no key run after the dash
    "risk-based-authentication-writeup",
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
