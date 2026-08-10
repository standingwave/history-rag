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
     body)
where `msgid` is the RFC Message-ID when the body file is readable, else a
hash of (from_addr, date, subject) — never Mail's ROWID, which renumbers on
resync. `body` is cooked plain text or None (an unreadable/undownloaded
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
"""
import glob, hashlib, os, re, sqlite3, sys
from email import message_from_bytes, policy
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from sources.common import SECRET_RE, group_paragraphs, snapshot_db

MAX_CHARS = 2000
GROUP_MAX = 700          # paragraph-group budget (obsidian's number)
WHOLE_MAX = 1200         # bodies at or under this merge into the envelope
RECIPIENTS_MAX = 8       # names shown in text; meta always carries all
PRUNE_WINDOW_DAYS = 30   # prune bound: chunks older than this are archive

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
                        account, body))
        return out
    finally:
        db.close()
        os.unlink(tmp)

_READERS = {"applemail": _read_applemail}

def _iso(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""

def _chunks(adapter, m):
    msgid, ts, from_name, from_addr, to_names, subject, mailbox, account, \
        body = m
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
                    "to": to_names, "subject": subject, "date": iso}}
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
