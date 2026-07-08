"""Per-source parsing and chunking edge cases."""
import textwrap
from datetime import datetime
from sources import browser, obsidian, shell
from sources.claude import _text_from_content

# ── shell histfile formats ───────────────────────────────────────────────────

def test_zsh_extended_multiline(tmp_path):
    p = tmp_path / "zh"
    p.write_text(": 1751400000:0;echo one\n: 1751400060:2;cat <<EOF\nline2\nEOF\n")
    assert shell._looks_zsh_extended(str(p))
    got = list(shell._parse_zsh_extended(str(p)))
    assert got[0] == (1751400000, "echo one")
    assert got[1][0] == 1751400060
    assert got[1][1] == "cat <<EOF\nline2\nEOF"     # continuation joined

def test_bash_epoch_lines(tmp_path):
    p = tmp_path / "bh"
    p.write_text("#1751400000\ngit status\nuname -a\n")
    got = list(shell._parse_bash(str(p)))
    assert got == [(1751400000, "git status"), (0, "uname -a")]

# ── claude content extraction ────────────────────────────────────────────────

def test_claude_drops_tool_results_keeps_text():
    assert _text_from_content("plain prompt", "user") == "plain prompt"
    tool_output = [{"type": "tool_result", "content": "x"}, {"type": "text", "text": "y"}]
    assert _text_from_content(tool_output, "user") == ""      # whole msg rejected
    reply = [{"type": "thinking", "thinking": "t"},
             {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
             {"type": "tool_use", "name": "Bash"}]
    assert _text_from_content(reply, "assistant") == "a\nb"

# ── browser URL cleaning ─────────────────────────────────────────────────────

def test_clean_url_strips_and_keeps():
    c = browser._clean_url
    assert c("https://a.com/path?utm=1#frag") == "https://a.com/path"
    assert c("https://www.youtube.com/watch?v=abc&pp=junk") == \
        "https://www.youtube.com/watch?v=abc"                 # default keep_params
    assert c("https://music.youtube.com/watch?v=x&list=l") == \
        "https://music.youtube.com/watch?v=x"                 # subdomain match
    assert c("https://www.youtube.com/results?search_query=a+b") == \
        "https://www.youtube.com/results?search_query=a+b"
    assert c("https://www.youtube.com/embed?v=abc") == \
        "https://www.youtube.com/embed"                       # v is /watch-scoped
    assert c("http://localhost:3000/x") is None
    assert c("file:///etc/passwd") is None
    assert c("chrome://settings") is None
    assert c("https://box.local/admin") is None

def test_keep_params_path_scoping():
    browser._keep_table = {"google.com/search": ["q"]}
    c = browser._clean_url
    assert c("https://www.google.com/search?q=cintas&sca=1") == \
        "https://www.google.com/search?q=cintas"
    # the redirect endpoint must NOT keep q — tracking links aren't searches
    assert c("https://www.google.com/url?q=https%3A%2F%2Fx.com%2Fabc") == \
        "https://www.google.com/url"

def test_search_text_announces_engines():
    s = browser._search_text
    assert s("https://www.google.com/search?q=stripe+stock") == \
        'Searched google.com for "stripe stock" — https://www.google.com/search?q=stripe+stock'
    assert s("https://www.youtube.com/results?search_query=clara+mattei") \
        .startswith('Searched youtube.com for "clara mattei"')
    assert s("https://www.youtube.com/watch?v=abc") is None    # identity, not search

# ── obsidian chunking ────────────────────────────────────────────────────────

NOTE = textwrap.dedent("""\
    ---
    date: 2025-03-15
    tags: [x]
    ---
    Preamble text.
    # Alpha
    alpha body
    ## Beta
    beta body
    #### Deep stays inside
    ## Beta
    second beta
    """)

def test_strip_frontmatter():
    body, date = obsidian._strip_frontmatter(NOTE)
    assert date == "2025-03-15"
    assert body.startswith("Preamble")
    assert obsidian._strip_frontmatter("no fm")[1] is None

def test_fm_iso_utc_normalized():
    # bare date -> local midnight expressed in UTC (offset-carrying string)
    ts = obsidian._fm_iso("2025-03-15")
    assert ts.endswith("+00:00")
    assert datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M") \
        == "2025-03-15 00:00"
    # explicit offset survives conversion
    assert obsidian._fm_iso("2025-03-15T10:00:00+02:00") \
        == "2025-03-15T08:00:00+00:00"
    assert obsidian._fm_iso("2025-03-15T08:00Z") == "2025-03-15T08:00:00+00:00"
    # nonsense that matches the date shape falls back cleanly
    assert obsidian._fm_iso("2025-13-45") == ""

def test_date_re_accepts_datetimes():
    for v in ["2025-03-15", "2025-03-15T10:00", "2025-03-15 10:00:00",
              "2025-03-15T10:00:00-07:00", "2025-03-15T10:00:00Z"]:
        m = obsidian._DATE_RE.search(f"date: {v}\n")
        assert m and m.group(1) == v, v

def test_sections_split():
    body, _ = obsidian._strip_frontmatter(NOTE)
    secs = list(obsidian._sections(body))
    headings = [h for h, _ in secs]
    assert headings == ["", "Alpha", "Beta", "Beta"]          # preamble + dup Beta
    assert "#### Deep stays inside" in secs[2][1]             # h4 not split out
    assert secs[1][1].startswith("# Alpha")                   # heading line kept
