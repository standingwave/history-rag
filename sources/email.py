"""Email source: local mail as recall — receipts ("when did I order X"),
correspondence, account events. First adapter reads Apple Mail's on-disk
store; no credentials, no network, same Full Disk Access grant the browser
and calendar sources already use.

Structured like the calendar source: per-adapter readers behind one
contract, config picks which run ([email].adapters, default [] so the
source no-ops until opted in — the Mail store exists for every macOS user,
so membership in ALL_SOURCES must not mean indexing by default). Locations
are namespaced "adapter:mailbox". A future gmail adapter (direct IMAP) is a
named seam; a Gmail account added to Mail.app arrives through the applemail
adapter with no new code — the adapter split is by store, not provider.

Each reader returns normalized messages
    (msgid, ts, from_name, from_addr, to_names, subject, mailbox, account,
     body, extra)
where `msgid` is the RFC Message-ID when the message is readable, else a
hash of (from_addr, date, subject) — never Mail's ROWID, which renumbers on
resync — and `extra` is adapter-private state merged into meta (gmail's
uid/uidv cursor). `body` is cooked plain text or None (an unreadable/undownloaded
body degrades the message to an envelope-only chunk, not a failure).
Cross-adapter and cross-mailbox dedup is by msgid, first wins (readers sort
by date, so the earliest filing keeps the message).

The applemail adapter catalogs via MailData/"Envelope Index" (WAL sqlite —
snapshot_db) and reads bodies from .emlx files, whose names are the
Envelope Index message ROWIDs sharded under Data/<n>/Messages/. An .emlx is
a byte-count line, an RFC822 message, then an Apple plist tail; partially
downloaded messages are .partial.emlx (headers only). Bodies prefer the
text/plain part, fall back to tag-stripped HTML, and drop quoted reply
chains and signatures — wrong stripping loses a paragraph, not the message.

Every message gets an envelope chunk (sender/recipients/date/subject —
the recall vocabulary); short bodies merge into it, long bodies add
paragraph-group chunks prefixed "From <sender>: <subject>" (the obsidian
title-prefix move). Secret filtering is per-paragraph: a credential-looking
subject drops the message, but a matching body paragraph drops only itself
— password-reset mail is exactly what account-recall queries want.

Mail's own store forgets legitimately ("keep only recent" settings,
server-side deletion), so PRUNE_WINDOW_DAYS bounds --prune --source email:
anything older than the window is archive the index has outlived. Failure
semantics copy calendar (shared source column + prune): one adapter
erroring while another yielded raises; every configured store missing or
unreadable yields nothing quietly (enabled before FDA is granted).

The gmail adapter is the one networked reader: direct IMAP with an app
password (GMAIL_APP_PASSWORD, env-only like every credential) against
[Gmail]/All Mail, read-only. It is INCREMENTAL: a cursor derived from the
index itself (max meta.uid for this account under the current UIDVALIDITY
— the digest precedent of a source reading the DB) means each run fetches
only new UIDs, capped at [gmail].max_fetch so a large backfill spreads
across scheduled runs instead of hanging one. First contact fetches
[gmail].backfill_days of history (0 = everything). A UIDVALIDITY change
resets the cursor; ids come from the RFC Message-ID, so a re-fetch
re-yields identical chunks and re-embeds nothing. Category noise
(promotions/social by default) is dropped via X-GM-RAW category searches;
labels map to the location mailbox (\\Inbox -> INBOX, \\Sent -> Sent,
first user label, else "All Mail"). Because an incremental adapter cannot
re-yield what it already indexed, --prune --source email is refused by
index.py while gmail is enabled — pruning would read "not re-yielded this
run" as "deleted". Misconfiguration (no user, no password) raises rather
than yielding nothing: solo it's a stderr skip, next to a yielding
adapter it fails the run where run health can see it.
"""
import glob, hashlib, imaplib, os, re, sqlite3, sys
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from sources.common import SECRET_RE, group_paragraphs, snapshot_db

MAX_CHARS = 2000
GROUP_MAX = 700          # paragraph-group budget (obsidian's number)
WHOLE_MAX = 1200         # bodies at or under this merge into the envelope
RECIPIENTS_MAX = 8       # names shown in text; meta always carries all
PRUNE_WINDOW_DAYS = 30   # prune bound: chunks older than this are archive
RAW_MAX = 512 * 1024     # gmail: larger raw messages index envelope-only

MAIL_ROOT = os.path.expanduser("~/Library/Mail")

# Mailboxes nobody means when asking what they were doing (compared against
# the decoded URL leaf, lowercased). User excludes add to, never replace.
# Automated/list mail is deliberately KEPT: receipts and confirmations are
# prime recall targets, and HTML-only marketing self-limits to envelopes.
_DEFAULT_EXCLUDES = {
    "junk", "spam", "bulk mail", "trash", "deleted messages",
    "drafts", "draft", "outbox", "sendlater", "import",
}

def _settings():
    import config
    adapters = [str(a) for a in config.get("email", "adapters", "", []) or []]
    excl = _DEFAULT_EXCLUDES | {
        str(m).lower()
        for m in config.get("email", "exclude_mailboxes", "", [])}
    return adapters, excl


def _envelope_index():
    """Newest V*/MailData/'Envelope Index' under MAIL_ROOT ('' if none) —
    older V dirs are migration relics that keep a bare MailData/."""
    hits = glob.glob(os.path.join(MAIL_ROOT, "V*", "MailData",
                                  "Envelope Index"))
    def vnum(p):
        m = re.search(r"V(\d+)$", os.path.dirname(os.path.dirname(p)))
        return int(m.group(1)) if m else -1
    return max(hits, key=vnum) if hits else ""

def _parse_mailbox_url(url: str):
    """'imap://<AccountUUID>/<Name...>' -> (uuid, 'Name/Sub'), decoded."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    return unquote(parts.netloc), unquote(parts.path).strip("/")

def _emlx_path(vdir, account, mailbox, rowid):
    """Locate <rowid>.emlx under the mailbox dir -> (path, partial) or
    (None, False). Nested mailboxes are nested .mbox dirs; shard dirs vary,
    so glob rather than hand-build the path."""
    boxdir = os.path.join(vdir, account,
                          *[seg + ".mbox" for seg in mailbox.split("/")])
    for suffix, partial in ((".emlx", False), (".partial.emlx", True)):
        hits = glob.glob(os.path.join(boxdir, "*", "Data", "**", "Messages",
                                      f"{rowid}{suffix}"), recursive=True)
        if hits:
            return hits[0], partial
    return None, False

class _HTMLText(HTMLParser):
    """Visible text of an HTML body; block-level tags become paragraph
    breaks so group_paragraphs has seams to work with."""
    _SKIP = {"script", "style", "head", "title"}
    _BLOCK = {"p", "div", "br", "tr", "li", "table",
              "h1", "h2", "h3", "h4", "h5", "h6"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n\n")
    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n\n")
    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

def _html_text(src: str) -> str:
    p = _HTMLText()
    try:
        p.feed(src)
    except Exception:
        return ""
    paras = [" ".join(seg.split())
             for seg in re.split(r"\n\s*\n", "".join(p.parts))]
    return "\n\n".join(seg for seg in paras if seg)

_WROTE_RE = re.compile(r"^On .{0,150}wrote:$")

def _strip_replies(text: str) -> str:
    """Drop quoted reply chains and signatures: '>'-quoted lines, everything
    from an 'On ... wrote:' attribution line on, and from a '-- ' marker."""
    out = []
    for line in text.splitlines():
        if _WROTE_RE.match(line.strip()) or line.rstrip() == "--":
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()

def _body_text(msg):
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return _strip_replies(part.get_content())
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            return _strip_replies(_html_text(part.get_content()))
    except Exception:       # undeclared charsets, broken MIME — body-less
        return None
    return None

def _read_emlx(vdir, account, mailbox, rowid):
    """-> (message_id or '', body_text or None)."""
    path, partial = _emlx_path(vdir, account, mailbox, rowid)
    if not path:
        return "", None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", None
    nl = raw.find(b"\n")
    try:
        count = int(raw[:nl])
        payload = raw[nl + 1:nl + 1 + count]    # plist tail excluded
    except ValueError:
        payload = raw[nl + 1:]
    try:
        msg = message_from_bytes(payload, policy=policy.default)
    except Exception:
        return "", None
    msgid = (msg.get("Message-ID") or "").strip().strip("<>")
    return msgid, (None if partial else _body_text(msg))

def _read_applemail(excludes):
    idx = _envelope_index()
    if not idx:
        return []
    db, tmp = snapshot_db(idx)
    try:
        vdir = os.path.dirname(os.path.dirname(idx))
        boxes = {}       # mailbox ROWID -> (account uuid, decoded name)
        for rowid, url in db.execute("SELECT ROWID, url FROM mailboxes"):
            account, name = _parse_mailbox_url(url or "")
            if (account and name
                    and name.rsplit("/", 1)[-1].lower() not in excludes):
                boxes[rowid] = (account, name)
        rcpt = {}        # message ROWID -> to/cc display names, in order
        for owner, addr, comment in db.execute(
                "SELECT r.message, a.address, a.comment FROM recipients r "
                "JOIN addresses a ON a.ROWID = r.address "
                "WHERE IFNULL(r.type, 0) IN (0, 1) ORDER BY r.position"):
            # never emails: a bare-address identity contributes its local
            # part, which is a name, not an address (calendar's rule)
            name = comment or (addr or "").split("@")[0]
            if name:
                rcpt.setdefault(owner, []).append(name)
        out = []
        for rowid, ts, addr, name, subject, mbid in db.execute(
                "SELECT m.ROWID, IFNULL(NULLIF(m.date_received, 0), "
                "IFNULL(m.date_sent, 0)), a.address, a.comment, s.subject, "
                "m.mailbox FROM messages m "
                "LEFT JOIN addresses a ON a.ROWID = m.sender "
                "LEFT JOIN subjects s ON s.ROWID = m.subject "
                "WHERE IFNULL(m.deleted, 0) = 0 ORDER BY 2"):
            box = boxes.get(mbid)
            if box is None:
                continue
            account, mailbox = box
            msgid, body = _read_emlx(vdir, account, mailbox, rowid)
            if not msgid:
                msgid = hashlib.sha256(
                    f"{addr}\0{ts}\0{subject}".encode()).hexdigest()[:32]
            out.append((msgid, ts or 0, name or "", addr or "",
                        rcpt.get(rowid, []), subject or "", mailbox,
                        account, body, {}))
        return out
    finally:
        db.close()
        os.unlink(tmp)


def _gmail_settings():
    import config
    user = str(config.get("gmail", "user", "CLAUDE_RAG_GMAIL_USER", "") or "")
    host = str(config.get("gmail", "host", "", "imap.gmail.com"))
    backfill = int(config.get("gmail", "backfill_days", "", 365))
    max_fetch = int(config.get("gmail", "max_fetch", "", 2000))
    cats = config.get("gmail", "exclude_categories", "",
                      ["promotions", "social"])
    return user, host, backfill, max_fetch, [str(c) for c in (cats or [])]

def _gmail_cursor(account: str, uidv: int) -> int:
    """Highest already-indexed UID for this account under this UIDVALIDITY,
    read from the index itself — the source stays stateless (digest
    precedent). 0 means backfill from scratch."""
    import config
    if not os.path.exists(config.DB_PATH):
        return 0
    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='chunks'").fetchone():
            return 0
        row = db.execute(
            "SELECT MAX(CAST(json_extract(meta,'$.uid') AS INTEGER)) "
            "FROM chunks WHERE source = 'email' "
            "AND json_extract(meta,'$.adapter') = 'gmail' "
            "AND json_extract(meta,'$.account') = ? "
            "AND json_extract(meta,'$.uidv') = ?", (account, uidv)).fetchone()
        return row[0] or 0
    finally:
        db.close()

def _gmail_uid_search(conn, *criteria) -> list:
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise OSError(f"gmail: search {criteria[0]} failed")
    return [int(u) for u in (data[0] or b"").split()]

def _gmail_labels(raw: bytes) -> list:
    return [(q or plain).decode("utf-8", "replace")
            for q, plain in re.findall(rb'"((?:[^"\\]|\\.)*)"|(\S+)', raw)]

def _gmail_mailbox(labels: list) -> str:
    if "\\Inbox" in labels:
        return "INBOX"
    if "\\Sent" in labels:
        return "Sent"
    user_labels = [l for l in labels if not l.startswith("\\")]
    return user_labels[0] if user_labels else "All Mail"

def _internal_ts(idate: str) -> float:
    try:
        return datetime.strptime(idate, "%d-%b-%Y %H:%M:%S %z").timestamp()
    except ValueError:
        return 0

def _gmail_fetch(conn, uids, account, uidv, excludes):
    """Fetch and normalize a batch of UIDs. Two rounds: metadata + headers
    for everything, full raw only for messages under RAW_MAX — BODY.PEEK[]
    of a 20MB attachment mail would be pure waste (attachments are a
    non-goal; its envelope still indexes)."""
    if not uids:
        return []
    seqset = ",".join(map(str, uids))
    typ, data = conn.uid(
        "FETCH", seqset, "(UID X-GM-MSGID X-GM-LABELS INTERNALDATE "
        "RFC822.SIZE BODY.PEEK[HEADER])")
    if typ != "OK":
        raise OSError("gmail: header fetch failed")
    meta = {}
    for item in data:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        head, payload = item[0], item[1]
        m = re.search(rb"UID (\d+)", head)
        if not m:
            continue
        gm = re.search(rb"X-GM-MSGID (\d+)", head)
        size = re.search(rb"RFC822\.SIZE (\d+)", head)
        labels = re.search(rb"X-GM-LABELS \(([^)]*)\)", head)
        idate = re.search(rb'INTERNALDATE "([^"]+)"', head)
        meta[int(m.group(1))] = (
            int(gm.group(1)) if gm else 0,
            _gmail_labels(labels.group(1) if labels else b""),
            int(size.group(1)) if size else 0,
            idate.group(1).decode() if idate else "", payload)
    small = sorted(u for u, v in meta.items() if v[2] <= RAW_MAX)
    raws = {}
    if small:
        typ, data = conn.uid("FETCH", ",".join(map(str, small)),
                             "(UID BODY.PEEK[])")
        if typ != "OK":
            raise OSError("gmail: body fetch failed")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                m = re.search(rb"UID (\d+)", item[0])
                if m:
                    raws[int(m.group(1))] = item[1]
    out = []
    for uid in sorted(meta):
        gm_msgid, labels, size, idate, header = meta[uid]
        mailbox = _gmail_mailbox(labels)
        if ("\\Draft" in labels or mailbox.lower() in excludes
                or any(l.lower() in excludes for l in labels
                       if not l.startswith("\\"))):
            continue
        raw = raws.get(uid)
        try:
            msg = message_from_bytes(raw or header, policy=policy.default)
        except Exception:
            continue
        addrs = getaddresses([str(msg.get("From", ""))])
        from_name, from_addr = addrs[0] if addrs else ("", "")
        to_names = []
        for name, addr in getaddresses(
                [str(h) for h in (msg.get_all("To", [])
                                  + msg.get_all("Cc", []))]):
            n = name or addr.split("@")[0]
            if n:
                to_names.append(n)
        subject = str(msg.get("Subject", "") or "")
        try:
            ts = parsedate_to_datetime(msg.get("Date")).timestamp()
        except (TypeError, ValueError):
            ts = _internal_ts(idate)
        msgid = ((msg.get("Message-ID") or "").strip().strip("<>")
                 or (str(gm_msgid) if gm_msgid else "")
                 or hashlib.sha256(
                     f"{from_addr}\0{ts}\0{subject}".encode()).hexdigest()[:32])
        body = _body_text(msg) if raw is not None else None
        out.append((msgid, ts or 0, from_name, from_addr, to_names, subject,
                    mailbox, account, body, {"uid": uid, "uidv": uidv}))
    return out

def _read_gmail(excludes):
    user, host, backfill_days, max_fetch, excl_cats = _gmail_settings()
    if not user:
        raise OSError("gmail: [gmail].user not set")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not password:
        raise OSError("gmail: GMAIL_APP_PASSWORD not set")
    conn = imaplib.IMAP4_SSL(host, timeout=60)
    try:
        try:
            conn.login(user, password)
            typ, data = conn.select('"[Gmail]/All Mail"', readonly=True)
            if typ != "OK":
                raise OSError(f"gmail: select failed: {data}")
            uidv = int((conn.response("UIDVALIDITY")[1] or [b"0"])[0]
                       or b"0")
            cursor = _gmail_cursor(user, uidv)
            if cursor:
                # n:* returns the highest existing UID even when n exceeds
                # it, so the > filter is load-bearing
                uids = [u for u in _gmail_uid_search(
                    conn, "UID", f"{cursor + 1}:*") if u > cursor]
            elif backfill_days:
                since = datetime.now(timezone.utc) - timedelta(
                    days=backfill_days)
                uids = _gmail_uid_search(conn, "SINCE",
                                         since.strftime("%d-%b-%Y"))
            else:
                uids = _gmail_uid_search(conn, "ALL")
            if uids and excl_cats:
                drop = set()
                for cat in excl_cats:
                    drop |= set(_gmail_uid_search(
                        conn, "X-GM-RAW", f'"category:{cat}"'))
                uids = [u for u in uids if u not in drop]
            uids = sorted(uids)[:max_fetch]
            out = []
            for i in range(0, len(uids), 50):
                out.extend(_gmail_fetch(conn, uids[i:i + 50], user, uidv,
                                        excludes))
            return out
        except imaplib.IMAP4.error as e:
            raise OSError(f"gmail: {e}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

_READERS = {"applemail": _read_applemail, "gmail": _read_gmail}

def _iso(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""

def _chunks(adapter, m):
    msgid, ts, from_name, from_addr, to_names, subject, mailbox, account, \
        body, extra = m
    if subject and SECRET_RE.search(subject):
        return
    sender = (f"{from_name} ({from_addr})" if from_name and from_addr
              else from_name or from_addr or "unknown sender")
    text = f"Email from {sender}"
    if to_names:
        shown = to_names[:RECIPIENTS_MAX]
        more = len(to_names) - len(shown)
        text += " to " + ", ".join(shown) + (f" +{more} more" if more else "")
    iso = _iso(ts)
    if iso:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        text += f" on {d:%Y-%m-%d} ({d:%A})"
    text += f": {subject or '(no subject)'} ({adapter}:{mailbox})."

    if body:
        body = "\n\n".join(p for p in re.split(r"\n\s*\n", body)
                           if p.strip() and not SECRET_RE.search(p))
    rec = {"source": "email", "timestamp": iso,
           "location": f"{adapter}:{mailbox}",
           "meta": {"adapter": adapter, "account": account,
                    "mailbox": mailbox, "msgid": msgid, "from": from_addr,
                    "to": to_names, "subject": subject, "date": iso,
                    **(extra or {})}}
    if body and len(body) <= WHOLE_MAX:
        text, body = f"{text}\n{body}", None
    yield ("email:" + hashlib.sha256(
        f"{adapter}\0{msgid}\0env".encode()).hexdigest()[:26],
        text[:MAX_CHARS], rec)
    if body:
        prefix = f"From {from_name or from_addr}: {subject}"
        for g, gtext in enumerate(group_paragraphs(body, GROUP_MAX)):
            yield ("email:" + hashlib.sha256(
                f"{adapter}\0{msgid}\0{g}".encode()).hexdigest()[:26],
                f"{prefix}\n{gtext}"[:MAX_CHARS], rec)

def iter_chunks():
    adapters, excludes = _settings()
    if not adapters:
        return
    unknown = [a for a in adapters if a not in _READERS]
    if unknown:
        raise ValueError(f"email: unknown adapter(s) {unknown}; "
                         f"known: {', '.join(_READERS)}")
    kept, failures, yielded_adapters = [], [], []
    seen = set()                     # msgid: first-configured adapter wins
    for adapter in adapters:
        try:
            msgs = _READERS[adapter](excludes)
        except (OSError, sqlite3.Error) as e:
            failures.append(f"{adapter}: {e}")
            continue
        if msgs:
            yielded_adapters.append(adapter)
        for m in msgs:
            if m[0] not in seen:
                seen.add(m[0])
                kept.append((adapter, m))
    if failures:
        # Partial failure must fail the source (prune safety); total failure
        # is the quiet no-op (store unreadable until FDA is granted).
        if yielded_adapters:
            raise RuntimeError("email: " + "; ".join(failures))
        print(f"email: skipping ({'; '.join(failures)})", file=sys.stderr)
        return
    for adapter, m in kept:
        yield from _chunks(adapter, m)
