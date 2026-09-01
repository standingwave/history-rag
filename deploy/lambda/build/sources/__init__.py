"""Content sources for the history RAG.

Each module exposes `iter_chunks()` yielding `(id, text, record)` where record is:

    {"source": str, "timestamp": iso str, "location": str, "meta": dict}

`id` must be stable across runs so indexing stays incremental. The driver in
index.py embeds the text and stores the record; it doesn't care what the source
is, so a new source is just a new module added to SOURCES there.
"""
