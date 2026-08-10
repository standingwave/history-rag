"""Gmail IMAP adapter against a scripted fake imaplib. Pins the wire-level
conventions the reader depends on: UID/X-GM-MSGID/X-GM-LABELS/RFC822.SIZE
fetch parsing, the n:* search gotcha (returns the highest UID even past the
end), cursor derivation from the index DB, UIDVALIDITY reset, category
exclusion via X-GM-RAW, and the two-round header/body fetch with the
RAW_MAX envelope-only fallback."""
import json
import os
import sqlite3
import types
import pytest
import config
import sources.email as em

USER = "g@example.com"


def rfc822(body="Hello there.\n", msgid="<gm-1@example.com>",
           subject="Test mail", date="Sun, 17 Jan 2016 03:06:39 +0000"):
    return (f"Message-ID: {msgid}\r\n"
            f"From: Karen Ho <karen@example.com>\r\n"
            f"To: Gabriel <me@example.com>\r\n"
            f"Subject: {subject}\r\n"
            f"Date: {date}\r\n"
            f"Content-Type: text/plain\r\n\r\n{body}").encode()


def header_of(raw: bytes) -> bytes:
    return raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"


class FakeError(Exception):
    pass


class FakeIMAP:
    """Scripted [Gmail]/All Mail. Class attrs are per-test state, reset by
    the fixture. `mailbox` maps uid -> dict(raw=..., labels=b'...',
    gm=int, size=int|None, idate=str)."""
    mailbox = {}
    categories = {}
    uidvalidity = 99
    fail_login = False
    searches = []

    def __init__(self, host, timeout=None):
        self.host = host

    def login(self, user, pw):
        if FakeIMAP.fail_login:
            raise FakeError("[AUTHENTICATIONFAILED] bad credentials")
        return ("OK", [b"authed"])

    def select(self, box, readonly=False):
        assert readonly, "gmail reader must never open read-write"
        return ("OK", [str(len(FakeIMAP.mailbox)).encode()])

    def response(self, key):
        assert key == "UIDVALIDITY"
        return (key, [str(FakeIMAP.uidvalidity).encode()])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            crit = args[1:]                    # args[0] is the charset (None)
            FakeIMAP.searches.append(crit)
            uids = sorted(FakeIMAP.mailbox)
            if crit[0] == "UID":
                lo = int(crit[1].split(":")[0])
                hits = [u for u in uids if u >= lo]
                # the n:* gotcha: past-the-end ranges match the last message
                return ("OK", [" ".join(
                    map(str, hits or uids[-1:])).encode()])
            if crit[0] == "X-GM-RAW":
                cat = crit[1].strip('"').removeprefix("category:")
                return ("OK", [" ".join(
                    map(str, FakeIMAP.categories.get(cat, []))).encode()])
            return ("OK", [" ".join(map(str, uids)).encode()])
        assert cmd == "FETCH"
        uids = [int(u) for u in args[0].split(",")]
        headers_only = "BODY.PEEK[HEADER]" in args[1]
        data = []
        for u in uids:
            m = FakeIMAP.mailbox.get(u)
            if not m:
                continue
            raw = m["raw"]
            size = m.get("size", len(raw))
            if headers_only:
                payload = header_of(raw)
                gm = m.get("gm", u * 111)
                labels = m.get("labels", "")
                idate = m.get("idate", "17-Jan-2016 03:06:39 +0000")
                head = (f'1 (UID {u} X-GM-MSGID {gm} X-GM-LABELS ({labels}) '
                        f'INTERNALDATE "{idate}" RFC822.SIZE {size} '
                        f'BODY[HEADER] {{{len(payload)}}}')
            else:
                payload = raw
                head = f"1 (UID {u} BODY[] {{{len(payload)}}}"
            data.append((head.encode(), payload))
            data.append(b")")
        return ("OK", data)

    def logout(self):
        return ("BYE", [b""])


@pytest.fixture
def gmail(monkeypatch):
    FakeIMAP.mailbox, FakeIMAP.categories = {}, {}
    FakeIMAP.uidvalidity, FakeIMAP.fail_login = 99, False
    FakeIMAP.searches = []
    monkeypatch.setattr(em, "imaplib", types.SimpleNamespace(
        IMAP4_SSL=FakeIMAP,
        IMAP4=types.SimpleNamespace(error=FakeError)))
    monkeypatch.setattr(config, "_FILE", {
        "email": {"adapters": ["gmail"]}, "gmail": {"user": USER}})
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    yield FakeIMAP
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)


def seed_index(rows):
    """Minimal chunks table so _gmail_cursor has something to read."""
    db = sqlite3.connect(config.DB_PATH)
    db.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY, text TEXT, "
               "source TEXT, timestamp TEXT, location TEXT, meta TEXT)")
    for i, meta in enumerate(rows):
        db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                   (f"email:seed{i}", "t", "email", "", "", json.dumps(meta)))
    db.commit()
    db.close()


def chunks():
    return list(em.iter_chunks())


# ── fetch + normalize ────────────────────────────────────────────────────────

def test_basic_message_becomes_envelope_chunk(gmail):
    gmail.mailbox[7] = {"raw": rfc822(), "labels": r"\Inbox"}
    (cid, text, rec), = chunks()
    assert text.startswith("Email from Karen Ho (karen@example.com) to "
                           "Gabriel on 2016-01-16 (Saturday): Test mail "
                           "(gmail:INBOX).")
    assert "Hello there." in text
    assert rec["timestamp"] == "2016-01-17T03:06:39+00:00"
    assert rec["meta"] == {"adapter": "gmail", "account": USER,
                           "mailbox": "INBOX", "msgid": "gm-1@example.com",
                           "from": "karen@example.com", "to": ["Gabriel"],
                           "subject": "Test mail",
                           "date": "2016-01-17T03:06:39+00:00",
                           "uid": 7, "uidv": 99}
    assert cid.startswith("email:")

def test_label_to_mailbox_mapping(gmail):
    gmail.mailbox[1] = {"raw": rfc822(msgid="<a@x>"), "labels": r"\Sent"}
    gmail.mailbox[2] = {"raw": rfc822(msgid="<b@x>"), "labels": '"Tax Stuff"'}
    gmail.mailbox[3] = {"raw": rfc822(msgid="<c@x>"), "labels": ""}
    locs = sorted(rec["location"] for _, _, rec in chunks())
    assert locs == ["gmail:All Mail", "gmail:Sent", "gmail:Tax Stuff"]

def test_draft_label_skipped(gmail):
    gmail.mailbox[1] = {"raw": rfc822(), "labels": r"\Draft"}
    assert chunks() == []

def test_excluded_user_label_skipped(gmail, monkeypatch):
    monkeypatch.setattr(config, "_FILE", {
        "email": {"adapters": ["gmail"], "exclude_mailboxes": ["Noise"]},
        "gmail": {"user": USER}})
    gmail.mailbox[1] = {"raw": rfc822(msgid="<a@x>"), "labels": r'\Inbox "Noise"'}
    gmail.mailbox[2] = {"raw": rfc822(msgid="<b@x>"), "labels": r"\Inbox"}
    (_, _, rec), = chunks()
    assert rec["meta"]["msgid"] == "b@x"

def test_oversized_message_envelope_only(gmail):
    gmail.mailbox[1] = {"raw": rfc822(body="never fetched\n"),
                        "labels": r"\Inbox", "size": em.RAW_MAX + 1}
    (_, text, _), = chunks()
    assert "Test mail" in text and "never fetched" not in text

def test_missing_date_header_falls_back_to_internaldate(gmail):
    raw = (b"Message-ID: <nd@x>\r\nFrom: a@b.com\r\n"
           b"Subject: no date\r\nContent-Type: text/plain\r\n\r\nhi\r\n")
    gmail.mailbox[1] = {"raw": raw, "labels": r"\Inbox",
                        "idate": "01-Feb-2015 10:00:00 +0000"}
    (_, _, rec), = chunks()
    assert rec["timestamp"] == "2015-02-01T10:00:00+00:00"


# ── cursor + search ──────────────────────────────────────────────────────────

def test_no_cursor_uses_since_backfill(gmail):
    gmail.mailbox[1] = {"raw": rfc822(), "labels": r"\Inbox"}
    chunks()
    assert gmail.searches[0][0] == "SINCE"

def test_backfill_days_zero_fetches_all(gmail, monkeypatch):
    monkeypatch.setattr(config, "_FILE", {
        "email": {"adapters": ["gmail"]},
        "gmail": {"user": USER, "backfill_days": 0}})
    gmail.mailbox[1] = {"raw": rfc822(), "labels": r"\Inbox"}
    chunks()
    assert gmail.searches[0][0] == "ALL"

def test_cursor_fetches_only_newer_uids(gmail):
    seed_index([{"adapter": "gmail", "account": USER, "uid": 5, "uidv": 99}])
    gmail.mailbox[5] = {"raw": rfc822(msgid="<old@x>"), "labels": r"\Inbox"}
    gmail.mailbox[8] = {"raw": rfc822(msgid="<new@x>"), "labels": r"\Inbox"}
    (_, _, rec), = chunks()
    assert rec["meta"]["msgid"] == "new@x"
    assert gmail.searches[0] == ("UID", "6:*")

def test_cursor_past_end_yields_nothing(gmail):
    # n:* returns the highest existing UID; the reader must filter it out
    seed_index([{"adapter": "gmail", "account": USER, "uid": 8, "uidv": 99}])
    gmail.mailbox[8] = {"raw": rfc822(), "labels": r"\Inbox"}
    assert chunks() == []

def test_uidvalidity_change_resets_cursor(gmail):
    seed_index([{"adapter": "gmail", "account": USER, "uid": 5, "uidv": 12}])
    gmail.mailbox[5] = {"raw": rfc822(), "labels": r"\Inbox"}
    assert len(chunks()) == 1                  # refetched despite uid <= 5
    assert gmail.searches[0][0] == "SINCE"

def test_max_fetch_caps_a_run(gmail, monkeypatch):
    monkeypatch.setattr(config, "_FILE", {
        "email": {"adapters": ["gmail"]},
        "gmail": {"user": USER, "max_fetch": 1}})
    gmail.mailbox[1] = {"raw": rfc822(msgid="<a@x>"), "labels": r"\Inbox"}
    gmail.mailbox[2] = {"raw": rfc822(msgid="<b@x>"), "labels": r"\Inbox"}
    (_, _, rec), = chunks()
    assert rec["meta"]["uid"] == 1             # lowest first: oldest-forward

def test_category_exclusion(gmail):
    gmail.mailbox[1] = {"raw": rfc822(msgid="<keep@x>"), "labels": r"\Inbox"}
    gmail.mailbox[2] = {"raw": rfc822(msgid="<promo@x>"), "labels": r"\Inbox"}
    gmail.categories["promotions"] = [2]
    (_, _, rec), = chunks()
    assert rec["meta"]["msgid"] == "keep@x"
    assert ("X-GM-RAW", '"category:promotions"') in gmail.searches
    assert ("X-GM-RAW", '"category:social"') in gmail.searches


# ── gates and failure semantics ──────────────────────────────────────────────

def test_missing_user_is_loud_but_solo_quiet(gmail, monkeypatch, capsys):
    monkeypatch.setattr(config, "_FILE", {"email": {"adapters": ["gmail"]}})
    assert chunks() == []
    assert "[gmail].user not set" in capsys.readouterr().err

def test_missing_password_solo_quiet_skip(gmail, monkeypatch, capsys):
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    assert chunks() == []
    assert "GMAIL_APP_PASSWORD not set" in capsys.readouterr().err

def test_auth_failure_becomes_oserror(gmail):
    gmail.fail_login = True
    with pytest.raises(OSError, match="AUTHENTICATIONFAILED"):
        em._read_gmail(set())

def test_dedup_across_adapters_first_configured_wins(gmail, monkeypatch):
    same = ("dup@x", 1, "A", "a@x", [], "s", "INBOX", "acct", None, {})
    monkeypatch.setitem(em._READERS, "applemail",
                        lambda excl: [same])
    monkeypatch.setattr(config, "_FILE", {
        "email": {"adapters": ["applemail", "gmail"]},
        "gmail": {"user": USER}})
    gmail.mailbox[1] = {"raw": rfc822(msgid="<dup@x>"), "labels": r"\Inbox"}
    (_, _, rec), = chunks()
    assert rec["meta"]["adapter"] == "applemail"

def test_prune_guard_refuses_email_with_gmail(gmail, monkeypatch):
    import index
    monkeypatch.setattr("sys.argv",
                        ["index.py", "--prune", "--source", "email"])
    with pytest.raises(SystemExit, match="gmail"):
        index.main()
