"""Phase 2 RAG: extract -> chunk -> embed (Chroma's local MiniLM, no API key) -> retrieve."""
import io, os
import chromadb
from pypdf import PdfReader
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

col = chromadb.PersistentClient(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_data")).get_or_create_collection(
    "docs", metadata={"hnsw:space": "cosine"})

def _docx_text(doc):
    """Paragraphs AND table cells, in document order (templates are often mostly tables)."""
    out = []
    for el in doc.element.body.iterchildren():
        if el.tag.endswith("}p"):
            out.append(Paragraph(el, doc).text)
        elif el.tag.endswith("}tbl"):
            for row in Table(el, doc).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip()
                    if t and t not in cells: cells.append(t)
                out.append(" | ".join(cells))
    return "\n".join(x for x in out if x.strip())

def extract(name, data):
    n = name.lower()
    if n.endswith(".pdf"):
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(PdfReader(io.BytesIO(data)).pages)]
    if n.endswith(".docx"):
        return [(1, _docx_text(Document(io.BytesIO(data))))]
    if n.endswith(".txt"):
        return [(1, data.decode("utf-8", "ignore"))]
    raise ValueError("Unsupported file type")

def chunk(text, size=800, overlap=120):
    text, out, i = " ".join(text.split()), [], 0
    while i < len(text):
        out.append(text[i:i + size]); i += size - overlap
    return out

def ingest(owner, scope, doc_id, name, data):
    ids, docs, metas = [], [], []
    for page, text in extract(name, data):
        for k, ch in enumerate(chunk(text)):
            ids.append(f"{doc_id}-{page}-{k}"); docs.append(ch)
            metas.append({"owner": owner, "scope": scope, "doc_id": doc_id, "filename": name, "page": page})
    if ids:
        col.add(ids=ids, documents=docs, metadatas=metas)
    return len(ids)

def retrieve(owner, scope, query, k=4, max_dist=0.6):
    """Owner+scope filter enforces isolation; max_dist is the relevance threshold (tune with evals)."""
    r = col.query(query_texts=[query], n_results=k,
                  where={"$and": [{"owner": owner}, {"scope": scope}]})
    return [{"text": d, "filename": m["filename"], "page": m["page"], "score": round(1 - x, 3)}
            for d, m, x in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]) if x <= max_dist]

def delete_doc(doc_id):
    col.delete(where={"doc_id": doc_id})
