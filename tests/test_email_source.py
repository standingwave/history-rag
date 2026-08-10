"""Email source against a fixture Envelope Index + .emlx tree (the
confirmed schema subset). Conventions pinned here were verified against the
live store: .emlx names are message ROWIDs sharded under Data/<n>/Messages/,
byte-count first line with a plist tail, recipients.type 0=to 1=cc,
mailbox urls imap://<AccountUUID>/<name> percent-encoded. TZ is
America/Los_Angeles (conftest)."""
import os
import sqlite3
import pytest
import sources.email as em

ACCT = "ACCT-UUID-1234"

SCHEMA = """
CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, sender INTEGER,
  subject INTEGER, date_sent INTEGER, date_received INTEGER,
  mailbox INTEGER, deleted INTEGER DEFAULT 0);
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER,
  address INTEGER, type INTEGER, position INTEGER);
"""

TS = 1452999999          # 2016-01-16 (Saturday) local, 2016-01-17 UTC


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Fixture V10 store with one account, INBOX + Archive + Junk, a sender
    and a recipient, and [email].adapters = ['applemail'] configured."""
    root = tmp_path / "Mail"
    mdata = root / "V10" / "MailData"
    mdata.mkdir(parents=True)
    db = sqlite3.connect(str(mdata / "Envelope Index"))
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO mailboxes VALUES (?, ?)", [
        (1, f"imap://{ACCT}/INBOX"),
        (2, f"imap://{ACCT}/Archive"),
        (3, f"imap://{ACCT}/Junk"),
        (4, f"imap://{ACCT}/Old%20Stuff"),
    ])
    db.executemany("INSERT INTO addresses VALUES (?, ?, ?)", [
        (1, "karen@example.com", "Karen Ho"),
        (2, "me@example.com", "Gabriel"),
        (3, "bare@example.com", ""),
    ])
    db.commit()
    monkeypatch.setattr(em, "MAIL_ROOT", str(root))
    import config
    monkeypatch.setattr(config, "_FILE",
                        {"email": {"adapters": ["applemail"]}})
    return db


def add_message(db, rowid, subject, ts=TS, sender=1, mailbox=1, deleted=0,
                to=(2,)):
    cur = db.execute("SELECT ROWID FROM subjects WHERE subject = ?",
                     (subject,)).fetchone()
    sid = cur[0] if cur else db.execute(
        "INSERT INTO subjects(subject) VALUES (?)", (subject,)).lastrowid
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
               (rowid, sender, sid, ts, ts, mailbox, deleted))
    for pos, addr in enumerate(to):
        db.execute("INSERT INTO recipients(message, address, type, position)"
                   " VALUES (?,?,0,?)", (rowid, addr, pos))
    db.commit()


def write_emlx(tmp_path, rowid, rfc822: bytes, mailbox="INBOX",
               partial=False, shard="4", plist=b"<plist>flags</plist>\n"):
    d = (tmp_path / "Mail" / "V10" / ACCT / f"{mailbox}.mbox" / "BOX-UUID"
         / "Data" / shard / "Messages")
    d.mkdir(parents=True, exist_ok=True)
    suffix = ".partial.emlx" if partial else ".emlx"
    (d / f"{rowid}{suffix}").write_bytes(
        b"%d\n%s%s" % (len(rfc822), rfc822, plist))


def rfc822(body="Hello there.\n", msgid="<msg-1@example.com>",
           ctype="text/plain"):
    return (f"Message-ID: {msgid}\r\n"
            f"From: Karen Ho <karen@example.com>\r\n"
            f"Subject: whatever\r\n"
            f"Content-Type: {ctype}\r\n\r\n{body}").encode()


def chunks():
    return list(em.iter_chunks())


# ── envelope chunks ──────────────────────────────────────────────────────────

def test_envelope_text_timestamp_meta(store, tmp_path):
    add_message(store, 1, "Lunch plans")
    write_emlx(tmp_path, 1, rfc822())
    (cid, text, rec), = chunks()
    assert text.startswith("Email from Karen Ho (karen@example.com) to "
                           "Gabriel on 2016-01-16 (Saturday): Lunch plans "
                           "(applemail:INBOX).")
    assert "Hello there." in text           # short body merged in
    assert rec["source"] == "email"
    assert rec["timestamp"] == "2016-01-17T03:06:39+00:00"
    assert rec["location"] == "applemail:INBOX"
    assert rec["meta"]["msgid"] == "msg-1@example.com"
    assert rec["meta"]["account"] == ACCT
    assert cid.startswith("email:")

def test_missing_emlx_envelope_only_and_stable_fallback_id(store):
    add_message(store, 1, "No body anywhere")
    (cid, text, _), = chunks()
    assert "No body anywhere" in text and "Hello" not in text
    assert [c[0] for c in em.iter_chunks()] == [cid]   # deterministic

def test_partial_emlx_headers_only(store, tmp_path):
    add_message(store, 1, "Big attachment")
    write_emlx(tmp_path, 1, rfc822(body="never downloaded\n"), partial=True)
    (cid, text, rec), = chunks()
    assert "never downloaded" not in text
    # the Message-ID header still names the chunk
    assert rec["meta"]["msgid"] == "msg-1@example.com"

def test_bare_address_recipient_uses_local_part(store, tmp_path):
    add_message(store, 1, "Hi", to=(3,))
    (_, text, _), = chunks()
    assert " to bare on " in text and "bare@example.com" not in text


# ── emlx parsing ─────────────────────────────────────────────────────────────

def test_byte_count_honored_plist_tail_ignored(store, tmp_path):
    add_message(store, 1, "Tail check")
    write_emlx(tmp_path, 1, rfc822(body="Real content.\n"),
               plist=b"NOT PART OF THE MESSAGE")
    (_, text, _), = chunks()
    assert "Real content." in text and "NOT PART" not in text

def test_multipart_prefers_plain_over_html(store, tmp_path):
    raw = (b"Message-ID: <mp@example.com>\r\n"
           b"Content-Type: multipart/alternative; boundary=B\r\n\r\n"
           b"--B\r\nContent-Type: text/plain\r\n\r\nplain wins\r\n"
           b"--B\r\nContent-Type: text/html\r\n\r\n<p>html loses</p>\r\n"
           b"--B--\r\n")
    add_message(store, 1, "Multipart")
    write_emlx(tmp_path, 1, raw)
    (_, text, _), = chunks()
    assert "plain wins" in text and "html loses" not in text

def test_html_only_body_tag_stripped(store, tmp_path):
    add_message(store, 1, "Receipt")
    write_emlx(tmp_path, 1, rfc822(
        body="<html><head><style>p{}</style></head><body>"
             "<p>Order #123 confirmed</p><script>evil()</script>"
             "</body></html>", ctype="text/html"))
    (_, text, _), = chunks()
    assert "Order #123 confirmed" in text
    assert "evil" not in text and "p{}" not in text

def test_no_shard_dir_still_found(store, tmp_path):
    add_message(store, 1, "Flat layout")
    d = (tmp_path / "Mail" / "V10" / ACCT / "INBOX.mbox" / "BOX-UUID"
         / "Data" / "Messages")
    d.mkdir(parents=True)
    raw = rfc822(body="found flat\n")
    (d / "1.emlx").write_bytes(b"%d\n%s" % (len(raw), raw))
    (_, text, _), = chunks()
    assert "found flat" in text


# ── reply hygiene ────────────────────────────────────────────────────────────

def test_quotes_attribution_and_signature_stripped(store, tmp_path):
    body = ("My reply.\n"
            "> the quoted part\n"
            "More of mine.\n"
            "On Jan 16, 2016, at 9:00 AM, Bob wrote:\n"
            "the whole old message\n")
    add_message(store, 1, "Re: plans")
    write_emlx(tmp_path, 1, rfc822(body=body))
    (_, text, _), = chunks()
    assert "My reply." in text and "More of mine." in text
    assert "quoted part" not in text and "old message" not in text

def test_signature_stripped(store, tmp_path):
    add_message(store, 1, "Sig")
    write_emlx(tmp_path, 1, rfc822(body="Content.\n-- \nGabe\n555-1234\n"))
    (_, text, _), = chunks()
    assert "Content." in text and "555-1234" not in text


# ── mailbox handling ─────────────────────────────────────────────────────────

def test_default_excludes_skip_junk(store, tmp_path):
    add_message(store, 1, "Kept", mailbox=1)
    add_message(store, 2, "V1AGRA", mailbox=3)
    texts = [t for _, t, _ in chunks()]
    assert len(texts) == 1 and "Kept" in texts[0]

def test_user_excludes_merge_with_defaults(store, tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "_FILE", {"email": {
        "adapters": ["applemail"], "exclude_mailboxes": ["Old Stuff"]}})
    add_message(store, 1, "Kept", mailbox=1)
    add_message(store, 2, "Ancient", mailbox=4)   # decoded 'Old Stuff' leaf
    add_message(store, 3, "Junky", mailbox=3)     # defaults still apply
    texts = [t for _, t, _ in chunks()]
    assert len(texts) == 1 and "Kept" in texts[0]

def test_deleted_rows_skipped(store):
    add_message(store, 1, "Gone", deleted=1)
    assert chunks() == []

def test_dedup_by_message_id_across_mailboxes(store, tmp_path):
    add_message(store, 1, "Same message", ts=TS, mailbox=1)
    add_message(store, 2, "Same message", ts=TS + 100, mailbox=2)
    write_emlx(tmp_path, 1, rfc822(msgid="<dup@example.com>"))
    write_emlx(tmp_path, 2, rfc822(msgid="<dup@example.com>"),
               mailbox="Archive")
    (_, _, rec), = chunks()
    assert rec["location"] == "applemail:INBOX"   # earliest filing wins


# ── secrets ──────────────────────────────────────────────────────────────────

def test_secret_subject_drops_message(store, tmp_path):
    add_message(store, 1, "Your password: hunter2")
    write_emlx(tmp_path, 1, rfc822())
    assert chunks() == []

def test_secret_paragraph_dropped_envelope_kept(store, tmp_path):
    add_message(store, 1, "Server details")
    write_emlx(tmp_path, 1, rfc822(
        body="The meeting is at noon.\n\npassword: hunter2\n\nSee you.\n"))
    (_, text, _), = chunks()
    assert "Server details" in text and "meeting is at noon" in text
    assert "hunter2" not in text and "See you." in text


# ── body chunking ────────────────────────────────────────────────────────────

def test_long_body_splits_into_prefixed_groups(store, tmp_path):
    paras = "\n\n".join(f"Paragraph {i}: " + "x" * 400 for i in range(6))
    add_message(store, 1, "Long thread")
    write_emlx(tmp_path, 1, rfc822(body=paras + "\n"))
    got = chunks()
    assert len(got) > 2
    env_text = got[0][1]
    assert env_text.startswith("Email from") and "Paragraph 0" not in env_text
    for cid, text, _ in got[1:]:
        assert text.startswith("From Karen Ho: Long thread\n")
    assert len({cid for cid, _, _ in got}) == len(got)   # distinct ids

def test_ids_stable_across_runs(store, tmp_path):
    paras = "\n\n".join(f"Paragraph {i}: " + "x" * 400 for i in range(4))
    add_message(store, 1, "Long thread")
    write_emlx(tmp_path, 1, rfc822(body=paras + "\n"))
    assert [c[0] for c in em.iter_chunks()] == [c[0] for c in em.iter_chunks()]


# ── gates and failure semantics ──────────────────────────────────────────────

def test_no_config_is_noop(monkeypatch):
    import config
    monkeypatch.setattr(config, "_FILE", {})
    assert chunks() == []

def test_unknown_adapter_raises(monkeypatch):
    import config
    monkeypatch.setattr(config, "_FILE",
                        {"email": {"adapters": ["outlook"]}})
    with pytest.raises(ValueError, match="unknown adapter"):
        chunks()

def test_missing_store_quiet_noop(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(em, "MAIL_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setattr(config, "_FILE",
                        {"email": {"adapters": ["applemail"]}})
    assert chunks() == []

def test_all_adapters_failing_quiet_noop(store, monkeypatch, capsys):
    def boom(excludes):
        raise OSError("permission denied")
    monkeypatch.setitem(em._READERS, "applemail", boom)
    assert chunks() == []
    assert "skipping" in capsys.readouterr().err

def test_partial_adapter_failure_raises(store, tmp_path, monkeypatch):
    add_message(store, 1, "Yielded fine")
    def boom(excludes):
        raise OSError("permission denied")
    monkeypatch.setitem(em._READERS, "fake", boom)
    import config
    monkeypatch.setattr(config, "_FILE",
                        {"email": {"adapters": ["applemail", "fake"]}})
    with pytest.raises(RuntimeError, match="fake: permission denied"):
        chunks()

def test_prune_window_declared(store):
    assert em.PRUNE_WINDOW_DAYS == 30
