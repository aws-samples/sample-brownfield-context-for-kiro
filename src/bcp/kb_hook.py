"""``bcp kb hook``: the Kiro ``UserPromptSubmit`` hook that puts verified facts and the code graph in context.

The hook reads the event JSON on stdin (``session_id``, ``hook_event_name``, ``cwd``, ``prompt``) and:

1. starts a Bedrock knowledge-base ``Retrieve`` for the prompt (top 5 facts) in a background thread,
   with ``AWS_EC2_METADATA_DISABLED=true`` and a 12 s budget measured from hook start. The default
   client is a SigV4-signed HTTPS call built with botocore and the stdlib only (no boto3 service-client
   build, which cost ~5.5 s of cold setup per prompt); it falls back to a boto3 client if signing or
   credential resolution is unavailable, and from there to the graph alone;
2. meanwhile loads the persistent component graph (``graph.json``, :mod:`bcp.graphq`, stdlib only) and
   matches components named in the prompt: exact class names, configuration keys and their constants,
   and single-word glossary terms from ``.kiro/context/glossary.md``;
3. adds the components the retrieved facts cite, then the graph neighbourhood of every matched
   component (dependents and dependencies up to ``hops`` hops, 2 by default, at most 3; affected
   programs for configuration keys);
4. writes the full result to ``<cwd>/.kiro/context/retrieved/latest.md``, prints a compact summary of at
   most :data:`SUMMARY_LIMIT` bytes (our Kiro CLI tests cut hook stdout at about 3,000 bytes; kiro.dev
   states no limit for UserPromptSubmit: https://kiro.dev/docs/hooks/actions/) tagged
   ``[verified KB <fact-id>]`` and ``[code graph <commit>]``, and appends one timing line to
   ``.kiro/context/retrieved/timing.jsonl`` (whether the graph-only fallback was used and why).

When the KB call fails or runs out of time the hook falls back to the graph alone. It never fails the
session: every error is caught and the exit code is always 0.

Settings come from ``<cwd>/.kiro/context/kb-hook.json`` (written by ``bcp generate`` when the config has
a ``kb`` section); command-line flags override them. The module runs as a script too
(``python <path>/bcp/kb_hook.py``), which is how the generated agent calls it: no package install and
no import of the rest of bcp.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__" and not __package__:  # run as a script: import bcp from the parent folder
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(_here))

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Sequence  # noqa: E402

SETTINGS_REL = ".kiro/context/kb-hook.json"
SETTINGS_SCHEMA = "bcp.kb-hook/1"
RETRIEVED_REL = ".kiro/context/retrieved"
LATEST_FILE = "latest.md"
TIMING_FILE = "timing.jsonl"
GLOSSARY_REL = ".kiro/context/glossary.md"
# Measured, not documented: our Kiro CLI tests cut UserPromptSubmit stdout at about 3,000 bytes; kiro.dev
# documents a 3,000-character cap only for the CLI PreCompact hook (https://kiro.dev/docs/hooks/types/).
SUMMARY_LIMIT = 2500
DEFAULT_TOP_K = 5
DEFAULT_TIMEOUT_S = 12.0  # bcp heuristic, below the hook timeout (seconds; https://kiro.dev/docs/hooks/)
DEFAULT_HOPS = 2
MAX_HOPS = 3
MAX_COMPONENTS = 8
MAX_ROWS_FULL = 20  # per list in latest.md, so the agent can read the whole file cheaply
MAX_PROMPT = 1000
ALLOWED_STATUSES = ("verified", "needs-edit", "derived")
FALLBACK_PYTHON = r"C:\Program Files\Python313\python.exe"
HEADER_KEYS = ("Fact:", "Type:", "Domain:", "Status:", "Review:", "Components:", "Evidence:", "Commit:",
               "Source hash", "Content hash", "Derived from:", "Source:", "Source file(s):", "Owning domain(s)")
CHANGED_PREFIX = "- What changed from the extraction:"
# derived-fact framing lines that say nothing about behaviour (glossary and feature-map facts)
BOILERPLATE_ENDS = ("cited as evidence by reviewed facts.",)
BOILERPLATE_STARTS = ("- Behaviour it takes part in", "Feature-to-component map for domain")
IDENT_RE = re.compile(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$\-]*)*")
GLOSSARY_RE = re.compile(r"^- \*\*([^*]+)\*\*:\s*(.*)$")
DOMAIN_RE = re.compile(r"\bdomain `([a-z0-9][a-z0-9-]*)`")
TABLE_RE = re.compile(r"\btable `([A-Za-z0-9_]+)`")

ClientFactory = Callable[[str, "dict[str, Any]"], Any]


# --------------------------------------------------------------------------- settings


def find_settings(cwd: Path) -> Path | None:
    """``.kiro/context/kb-hook.json`` in ``cwd`` or up to three parent folders."""
    for folder in [cwd, *list(cwd.parents)[:3]]:
        candidate = folder / SETTINGS_REL
        if candidate.is_file():
            return candidate
    return None


def load_settings(path: Path | None) -> dict[str, Any]:
    """Settings file contents (empty when missing or unreadable)."""
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_python(configured: str | None = None) -> str:
    """Interpreter the generated hook command uses: the configured one, else this interpreter when it
    can import boto3, else :data:`FALLBACK_PYTHON` when it exists, else this interpreter."""
    if configured:
        return configured
    import importlib.util

    if importlib.util.find_spec("boto3") is not None:
        return sys.executable
    if Path(FALLBACK_PYTHON).is_file():
        return FALLBACK_PYTHON
    return sys.executable


def hook_script() -> str:
    """Absolute path of this module (the generated command runs it as a script)."""
    return str(Path(__file__).resolve())


def hook_command(python: str) -> str:
    """``"<python>" "<script>"``: the command form proven end to end in Kiro CLI."""
    return f'"{python}" "{hook_script()}"'


# --------------------------------------------------------------------------- KB retrieve


def boto3_client_factory(region: str, options: dict[str, Any]) -> Any:
    """boto3 ``bedrock-agent-runtime`` client with tight timeouts (imported lazily: ~1.5 s, plus the
    service-model load that cost ~5.5 s per prompt). The fallback for :func:`default_client_factory`
    when SigV4 signing or credential resolution is not available."""
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    import boto3
    from botocore.config import Config

    budget = max(1.0, float(options.get("budget_s") or DEFAULT_TIMEOUT_S))
    config = Config(connect_timeout=min(5.0, budget), read_timeout=budget,
                    retries={"total_max_attempts": 2, "mode": "standard"})
    kwargs: dict[str, Any] = {"region_name": region, "config": config}
    if options.get("endpoint_url"):
        kwargs["endpoint_url"] = options["endpoint_url"]
    return boto3.client("bedrock-agent-runtime", **kwargs)


# Endpoint confirmed from the local botocore service model: host bedrock-agent-runtime.{region}.amazonaws.com,
# SigV4 signing name "bedrock", POST /knowledgebases/{id}/retrieve, JSON request and response.
def signed_retrieve_request(region: str, kb_id: str, query: str, top_k: int, credentials: Any,
                            endpoint: str | None = None) -> Any:
    """A SigV4-signed ``AWSRequest`` for a Bedrock KB ``Retrieve`` call, built with botocore only (no
    boto3 service client). The signed request carries ``Authorization`` and ``X-Amz-Date`` headers."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    base = (endpoint or f"https://bedrock-agent-runtime.{region}.amazonaws.com").rstrip("/")
    url = f"{base}/knowledgebases/{kb_id}/retrieve"
    body = json.dumps({"retrievalQuery": {"text": query},
                       "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": top_k}}})
    request = AWSRequest(method="POST", url=url, data=body.encode("utf-8"),
                        headers={"Content-Type": "application/json"})
    SigV4Auth(credentials, "bedrock", region).add_auth(request)
    return request


def https_post(url: str, body: Any, headers: dict[str, str], timeout: float) -> str:
    """POST over HTTPS only (stdlib ``http.client``) and return the response text. Any other scheme is
    refused, so no ``file:`` or custom-scheme URL can be opened. HTTP status 400 and above raises."""
    import http.client
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing non-HTTPS endpoint (scheme {parts.scheme!r})")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        if resp.status >= 400:
            raise RuntimeError(f"Retrieve failed: HTTP {resp.status}: {data[:300]}")
        return data
    finally:
        conn.close()


class _SignedRetrieveClient:
    """Runs Bedrock ``Retrieve`` over HTTPS with a SigV4-signed request (botocore + stdlib ``http.client``
    only), so the hook avoids boto3's per-process service-client build (the dominant ~5.5 s of cold
    setup). Credentials are resolved eagerly in the factory so a credential failure degrades to boto3."""

    def __init__(self, region: str, options: dict[str, Any]) -> None:
        import botocore.session

        self.region = region
        self.budget = max(1.0, float(options.get("budget_s") or DEFAULT_TIMEOUT_S))
        self.endpoint = options.get("endpoint_url")
        creds = botocore.session.get_session().get_credentials()
        if creds is None:
            raise RuntimeError("no AWS credentials resolved")
        self.credentials = creds.get_frozen_credentials()

    def retrieve(self, knowledgeBaseId: str, retrievalQuery: dict[str, Any],  # noqa: N803 - boto3 param names
                 retrievalConfiguration: dict[str, Any]) -> dict[str, Any]:
        query = str(retrievalQuery["text"])
        top_k = int(retrievalConfiguration["vectorSearchConfiguration"]["numberOfResults"])
        request = signed_retrieve_request(self.region, knowledgeBaseId, query, top_k, self.credentials,
                                          self.endpoint)
        prepared = request.prepare()
        return json.loads(https_post(prepared.url, prepared.body, dict(prepared.headers), self.budget))


def default_client_factory(region: str, options: dict[str, Any]) -> Any:
    """Warm default: a SigV4-signed HTTPS ``Retrieve`` client (no boto3 service-client build). Degrades
    to the boto3 client when SigV4 signing or credential resolution is unavailable, and from there the
    ``Retriever`` falls back to the graph alone, so the hook never fails the session."""
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        return _SignedRetrieveClient(region, options)
    except Exception:  # noqa: BLE001 - any signing/credential failure degrades to boto3
        return boto3_client_factory(region, options)


class Retriever(threading.Thread):
    """Runs one ``Retrieve`` in a daemon thread so the caller can stop waiting at the deadline."""

    def __init__(self, kb_id: str, region: str, query: str, top_k: int, factory: ClientFactory,
                 options: dict[str, Any]) -> None:
        super().__init__(name="bcp-kb-retrieve", daemon=True)
        self.kb_id, self.region, self.query, self.top_k = kb_id, region, query, top_k
        self.factory, self.options = factory, options
        self.results: list[dict[str, Any]] | None = None
        self.error: str | None = None
        self.phases: dict[str, int] = {}

    def run(self) -> None:  # pragma: no branch - exceptions are the result
        t0 = time.perf_counter()
        try:
            client = self.factory(self.region, self.options)
            t1 = time.perf_counter()
            self.phases["client_ms"] = round((t1 - t0) * 1000)
            resp = client.retrieve(
                knowledgeBaseId=self.kb_id, retrievalQuery={"text": self.query},
                retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": self.top_k}})
            self.phases["call_ms"] = round((time.perf_counter() - t1) * 1000)
            self.results = list(resp.get("retrievalResults") or [])
        except Exception as exc:  # noqa: BLE001 - any failure means graph-only fallback
            self.error = f"{type(exc).__name__}: {str(exc)[:200]}"


def _unquote(value: Any) -> str:
    # Bedrock returns list metadata items JSON-encoded: '"EncounterServiceImpl"'
    return str(value).strip().strip('"').strip("'").strip()


def _meta_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [u for u in (_unquote(v) for v in value if v is not None) if u]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [u for u in (_unquote(v) for v in parsed) if u]
        except ValueError:
            pass
    return [u for u in (_unquote(v) for v in text.split(",")) if u]


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def fact_brief(text: str, limit: int = 120) -> tuple[str, str]:
    """(title, short statement) of a fact document: the reviewer's correction for a needs-edit fact,
    else the first lines of the rule body (header bullets skipped)."""
    title, body, changed = "", [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        if stripped.startswith("#"):
            title = title or stripped.lstrip("#").strip()
            continue
        if stripped.startswith(CHANGED_PREFIX):
            changed = stripped[len(CHANGED_PREFIX):].strip()
            continue
        if stripped.startswith("- ") and stripped[2:].startswith(HEADER_KEYS):
            continue
        if stripped.endswith(BOILERPLATE_ENDS) or stripped.startswith(BOILERPLATE_STARTS):
            continue
        if len(" ".join(body)) < limit:
            body.append(stripped)
    if changed:
        return title, "corrected: " + _cut(changed, limit)
    return title, _cut(" ".join(body), limit)


def parse_results(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Fact records from ``retrievalResults``; results whose status is not allowed are dropped."""
    facts, skipped = [], 0
    for r in raw:
        meta = r.get("metadata") or {}
        status = str(meta.get("status") or "")
        if status not in ALLOWED_STATUSES:
            skipped += 1
            continue
        text = str((r.get("content") or {}).get("text") or "").strip()
        title, statement = fact_brief(text)
        fact_id = str(meta.get("fact_id") or "") or title or "unknown"
        facts.append({
            "fact_id": fact_id, "status": status, "score": round(float(r.get("score") or 0.0), 4),
            "domain": meta.get("domain"), "type": meta.get("fact_type"),
            "components": _meta_list(meta.get("components")), "evidence": _meta_list(meta.get("evidence")),
            "commit": meta.get("commit"), "title": title, "statement": statement, "text": text,
            "uri": ((r.get("location") or {}).get("s3Location") or {}).get("uri"),
        })
    return facts, skipped


# --------------------------------------------------------------------------- component matching


def load_glossary(path: Path) -> dict[str, list[str]]:
    """Single-identifier glossary terms (lowercase) → the glossary lines that define them."""
    terms: dict[str, list[str]] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return terms
    for line in text.splitlines():
        match = GLOSSARY_RE.match(line.strip())
        if not match:
            continue
        term = match.group(1).strip()
        if re.fullmatch(r"[A-Za-z][\w.\-]{2,}", term):
            terms.setdefault(term.lower(), []).append(match.group(2))
    return terms


class Matcher:
    """Resolves names to graph nodes: exact names in the prompt, glossary terms, fact components."""

    def __init__(self, graph: Any, glossary: dict[str, list[str]]) -> None:
        self.graph = graph
        self.glossary = glossary
        self.exact: dict[str, list[str]] = {}
        for node_id, node in graph.nodes.items():
            kind = node.get("type")
            names: list[str] = []
            if kind == "class":
                names = [node.get("name"), node.get("fqn")]
            elif kind == "config":
                names = [node.get("key"), *(node.get("constants") or [])]
            elif kind == "endpoint" and node.get("kind") != "integration":
                names = [node.get("name")]
            for name in names:
                if name and len(str(name)) >= 3:
                    self.exact.setdefault(str(name), []).append(node_id)
        self.in_degree = {n: len(es) for n, es in graph.inc.items()}

    def _pick(self, ids: list[str]) -> list[str]:
        demo = [i for i in ids if self.graph.nodes[i].get("demo")]
        return sorted(demo or ids)[:2]

    def component(self, name: str) -> list[str]:
        """Node ids for a fact's component name (class simple or qualified name, key or constant)."""
        if name in self.exact:
            return self._pick(self.exact[name])
        try:
            return [self.graph.resolve(name)]
        except Exception:  # noqa: BLE001 - unknown or ambiguous names are skipped
            return []

    def from_prompt(self, prompt: str) -> list[tuple[str, str]]:
        """(node id, how matched) for exact names, then glossary terms, in prompt order."""
        found: list[tuple[str, str]] = []
        tokens = IDENT_RE.findall(prompt)
        for tok in tokens:
            for cand in (tok, tok.rstrip(".")):
                if cand in self.exact:
                    found += [(i, f"name `{cand}` in the prompt") for i in self._pick(self.exact[cand])]
                    break
        for tok in tokens:
            low = tok.lower()
            for term in (low, low[:-1] if low.endswith("s") else None, low[:-2] if low.endswith("es") else None):
                if not term or term not in self.glossary:
                    continue
                found += [(i, f"glossary term `{term}`") for i in self._glossary_nodes(term)]
                break
        return found

    def _glossary_nodes(self, term: str) -> list[str]:
        ids: list[str] = []
        try:
            node_id = self.graph.resolve(term)
            if self.graph.nodes[node_id]["type"] in ("class", "config"):
                return [node_id]
        except Exception:  # noqa: BLE001
            pass
        for definition in self.glossary[term]:
            for slug in DOMAIN_RE.findall(definition):
                if f"domain:{slug}" in self.graph.nodes:
                    ids.append(f"domain:{slug}")
            for table in TABLE_RE.findall(definition):
                if f"table:{table}" in self.graph.nodes:
                    ids.append(f"table:{table}")
        return list(dict.fromkeys(ids))[:2]


def _short(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("key") or node["id"])


def neighbourhood(graph: Any, matcher: Matcher, node_id: str, hops: int) -> dict[str, Any]:
    """Dependents, dependencies (or domain members) and, for configuration keys, affected programs."""
    node = graph.nodes[node_id]
    out: dict[str, Any] = {"id": node_id, "type": node["type"], "name": _short(node),
                           "domains": node.get("domains") or [], "path": node.get("path")}
    rank = matcher.in_degree
    home = set(node.get("domains") or [])
    if node["type"] == "domain":
        members = [e["from"] for e in graph.inc.get(node_id, []) if e["type"] == "in-domain"]
        members.sort(key=lambda m: (-rank.get(m, 0), m))
        out["members"] = [{"id": m, "name": _short(graph.nodes[m]), "dependents": rank.get(m, 0)}
                          for m in members]
        return out
    deps = graph.dependents(node_id, depth=min(hops, MAX_HOPS))
    needs = graph.dependencies(node_id, depth=min(hops, MAX_HOPS))
    for key, res in (("dependents", deps), ("dependencies", needs)):
        # nearest first; then neighbours in the component's own domain(s); then the most depended-on
        rows = sorted(res["results"], key=lambda r: (r["depth"], not (home & set(r.get("domains") or [])),
                                                     -rank.get(r["id"], 0), r["id"]))
        out[key] = {"count": res["count"], "by_domain": res["by_domain"], "rows": rows}
    if node["type"] == "config":
        out["programs"] = graph.affected_programs(node_id, depth=1)
    return out


# --------------------------------------------------------------------------- output


def _evidence_short(evidence: list[str]) -> str:
    if not evidence:
        return ""
    first = evidence[0].replace("\\", "/").rsplit("/", 1)[-1]
    return first + (f" +{len(evidence) - 1}" if len(evidence) > 1 else "")


def _counts(by_domain: dict[str, int], n: int = 3) -> str:
    top = sorted(by_domain.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return ", ".join(f"{d} {c}" for d, c in top)


def fact_freshness(fact: dict[str, Any], commit: str | None) -> str:
    """Freshness status of a retrieved fact. A retrieved fact comes from the indexed ``verified/``
    prefix, so it is ``verified-fresh`` by default; it is reported ``verified-stale`` (a re-review or a
    ``bcp kb refresh`` is due) when its recorded commit is known and differs from the current code-graph
    commit. The graph-only fallback and the no-fact case are reported as ``unverified`` by the caller."""
    fc = str(fact.get("commit") or "").strip()
    if commit and fc and fc != str(commit).strip():
        return "verified-stale"
    return "verified-fresh"


def summary_lines(facts: list[dict[str, Any]], comps: list[dict[str, Any]], commit: str | None) -> list[str]:
    """One line per fact and per component (the caller fits them into the byte limit)."""
    lines = []
    for f in facts:
        status = "" if f["status"] == "verified" else f" ({f['status']})"
        ev = _evidence_short(f["evidence"])
        title = _cut(f["title"], 80)
        text = f"{title}: {f['statement']}" if f["statement"] else title
        lines.append(f"- [verified KB {f['fact_id']}] [{fact_freshness(f, commit)}]{status} {text}"
                      + (f" (evidence {ev})" if ev else ""))
    tag = f"[code graph {(commit or 'unknown')[:12]}]"
    for c in comps:
        where = f" ({', '.join(c['domains'][:2])})" if c.get("domains") else ""
        if c["type"] == "domain":
            names = ", ".join(m["name"] for m in c["members"][:6])
            lines.append(f"- {tag} domain {c['name']}: {len(c['members'])} classes; most depended-on: {names}")
            continue
        dep, need = c["dependents"], c["dependencies"]
        part = f"- {tag} {c['type']} {c['name']}{where}: {dep['count']} dependents"
        if dep["count"]:
            part += f" ({_counts(dep['by_domain'])}): " + ", ".join(_short(r) for r in dep["rows"][:4])
        part += f"; depends on {need['count']}"
        if need["count"]:
            part += ": " + ", ".join(_short(r) for r in need["rows"][:3])
        if c["type"] == "config":
            prog = c["programs"]
            groups = "; ".join(f"{'/'.join(g['programs'])}={g['effective']}" for g in prog.get("groups", [])[:3])
            readers = ", ".join(_short(r) for r in prog.get("readers", [])[:3])
            part += (f"; programs {'differ' if prog.get('differs') else 'same'}: {groups}"
                     + (f"; read by {readers}" if readers else ""))
        lines.append(part)
    return lines


def _clip(line: str, limit: int) -> str:
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    return data[: max(0, limit - 4)].decode("utf-8", "ignore").rstrip() + " ..."


def fit_summary(header: str, body: list[str], footer: list[str], limit: int = SUMMARY_LIMIT) -> str:
    """Header, as many body lines as fit (each at most 400 bytes), then the footer; at most ``limit``
    bytes in UTF-8."""
    foot = "\n".join(footer)
    used = len(header.encode("utf-8")) + 1 + len(foot.encode("utf-8")) + 1
    kept, dropped = [], 0
    for line in body:
        line = _clip(line, 400)
        size = len(line.encode("utf-8")) + 1
        if used + size > limit - 60:
            dropped += 1
            continue
        kept.append(line)
        used += size
    if dropped:
        kept.append(f"- ({dropped} more lines in the full file)")
    text = "\n".join([header, *kept, foot])
    return _clip(text, limit)


def _row_md(row: dict[str, Any]) -> str:
    via = row.get("via") or {}
    ev = (via.get("evidence") or [""])[0]
    doms = ", ".join(row.get("domains") or [])
    return (f"  - d{row['depth']} `{_short(row)}` ({row['type']}{', ' + doms if doms else ''}) via "
            f"{via.get('type')} {via.get('from')} -> {via.get('to')}" + (f" [{ev}]" if ev else ""))


def render_full(ctx: dict[str, Any], facts: list[dict[str, Any]], comps: list[dict[str, Any]]) -> str:
    """The full ``latest.md`` text."""
    commit = ctx.get("commit") or "unknown"
    lines = [
        "<!-- written by bcp kb hook on every prompt; overwritten; not reviewed content -->",
        "# Retrieved context",
        f"- Written: {ctx['ts']} | session {ctx.get('session_id') or '-'}",
        f"- Prompt: {ctx['prompt'][:300]!r}",
        f"- Knowledge base: {ctx.get('kb_id') or 'not configured'} ({ctx.get('region') or '-'}): "
        + (f"{len(facts)} facts in {ctx['kb_ms']} ms" if not ctx["fallback"] else
           f"graph-only fallback ({ctx['fallback_reason']})"),
        f"- Code graph: {ctx.get('graph') or 'not found'} @ {commit}"
        + (f" ({ctx['graph_error']})" if ctx.get("graph_error") else ""),
        "",
        "Provenance: `[verified KB <fact-id>]` is a reviewed fact from the validated knowledge base; it is a "
        "map, not proof: the code is the truth, and the mandatory code check (authorization, transactions and "
        "error end states, concurrency, absolute claims) still applies. `[code graph <commit>]` is a mechanical "
        "dependency edge read from the code at that commit, with its evidence line.",
        "",
        "## Verified facts",
    ]
    if not facts:
        lines.append("(none retrieved)")
    for i, f in enumerate(facts, 1):
        lines += [
            "",
            f"### {i}. [verified KB {f['fact_id']}] score {f['score']}, status {f['status']}, "
            f"{fact_freshness(f, ctx.get('commit'))}",
            f"- Components: {', '.join(f['components']) or '-'} | evidence: {', '.join(f['evidence']) or '-'}"
            f" | commit {f.get('commit') or '-'}",
            "",
            f["text"],
        ]
    lines += ["", f"## Components and graph neighbourhood [code graph {commit[:12]}]"]
    if not comps:
        lines.append("(no component matched)")
    for c in comps:
        lines += ["", f"### {c['type']} `{c['name']}` ({c['id']})", f"- Matched: {'; '.join(c['matched'])}"]
        if c.get("path"):
            lines.append(f"- Path: {c['path']}")
        if c["type"] == "domain":
            lines.append(f"- Classes in the domain ({len(c['members'])}, most depended-on first):")
            lines += [f"  - `{m['name']}` ({m['dependents']} dependents)" for m in c["members"][:MAX_ROWS_FULL]]
            continue
        for key, label in (("dependents", "Dependents (what may break if it changes)"),
                           ("dependencies", "Dependencies (what it needs)")):
            block = c[key]
            lines.append(f"- {label}: {block['count']} within {ctx['hops']} hops"
                         + (f"; by domain: {_counts(block['by_domain'], 10)}" if block["by_domain"] else ""))
            lines += [_row_md(r) for r in block["rows"][:MAX_ROWS_FULL]]
            if block["count"] > MAX_ROWS_FULL:
                lines.append(f"  - ... {block['count'] - MAX_ROWS_FULL} more; search the code or "
                             "`.kiro/context/dependency-map.md` for the rest")
        if c["type"] == "config":
            prog = c["programs"]
            lines.append(f"- Affected programs (differs: {prog.get('differs')}; {prog.get('note')}):")
            lines += [f"  - {'/'.join(g['programs'])}: {g['effective']}" for g in prog.get("groups", [])]
            lines.append("- Read by: " + (", ".join(_short(r) for r in prog.get("readers", [])) or "-"))
            lines.append("- Impacted through readers: "
                         + (", ".join(_short(r) for r in prog.get("impacted", [])[:MAX_ROWS_FULL]) or "-"))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the hook


def _read_event(stdin_text: str) -> dict[str, Any]:
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(path: str | None, cwd: Path) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else cwd / p


def run_hook(stdin_text: str, overrides: dict[str, Any] | None = None,
             factory: ClientFactory | None = None, clock: Callable[[], float] = time.perf_counter
             ) -> tuple[str, dict[str, Any], Retriever | None]:
    """Run the hook; returns (summary text, timing record, the retriever thread or None). Never raises."""
    t0 = clock()
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    event = _read_event(stdin_text)
    cwd = Path(overrides.get("cwd") or event.get("cwd") or os.getcwd())
    prompt = str(event.get("prompt") or "")[:MAX_PROMPT]
    settings_path = _resolve(overrides.get("settings"), cwd) if overrides.get("settings") else find_settings(cwd)
    settings = {**load_settings(settings_path), **overrides}
    root = settings_path.parents[2] if settings_path and not overrides.get("settings") else cwd
    timeout_s = float(settings.get("timeout_s") or DEFAULT_TIMEOUT_S)
    top_k = int(settings.get("top_k") or DEFAULT_TOP_K)
    hops = max(1, min(MAX_HOPS, int(settings.get("hops") or DEFAULT_HOPS)))
    kb_id, region = settings.get("kb_id"), settings.get("region") or "us-east-1"
    out_dir = root / RETRIEVED_REL
    ctx: dict[str, Any] = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "session_id": event.get("session_id"), "prompt": prompt, "kb_id": kb_id,
                           "region": region, "hops": hops, "fallback": False, "fallback_reason": None,
                           "kb_ms": None, "graph": None, "commit": None, "graph_error": None}
    record: dict[str, Any] = {"ts": ctx["ts"], "session_id": event.get("session_id"), "kb_id": kb_id,
                              "prompt_chars": len(prompt), "timeout_s": timeout_s}
    retriever: Retriever | None = None
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        if kb_id and prompt.strip():
            retriever = Retriever(str(kb_id), str(region), prompt, top_k, factory or default_client_factory,
                                  {"budget_s": timeout_s, "endpoint_url": settings.get("endpoint_url")})
            retriever.start()
        # graph work runs while the KB call is in flight
        tg = clock()
        graph = matcher = None
        graph_path = _resolve(settings.get("graph") or ".kiro/context/graph.json", root)
        try:
            from bcp.graphq import Graph

            graph = Graph.load(graph_path)  # type: ignore[arg-type]
            ctx["graph"], ctx["commit"] = str(graph_path), graph.commit
            glossary = load_glossary(_resolve(settings.get("glossary") or GLOSSARY_REL, root))  # type: ignore[arg-type]
            matcher = Matcher(graph, glossary)
        except Exception as exc:  # noqa: BLE001 - no graph: facts only
            ctx["graph_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        prompt_hits = matcher.from_prompt(prompt) if matcher else []
        graph_ms = round((clock() - tg) * 1000)
        # wait for the KB within the budget measured from hook start
        facts: list[dict[str, Any]] = []
        skipped = 0
        if retriever is None:
            ctx["fallback"] = True
            ctx["fallback_reason"] = "no kb_id configured" if not kb_id else "empty prompt"
        else:
            retriever.join(max(0.0, timeout_s - (clock() - t0)))
            ctx["kb_ms"] = round((clock() - t0) * 1000)
            if retriever.is_alive():
                ctx["fallback"], ctx["fallback_reason"] = True, f"KB retrieve timed out ({timeout_s:g} s)"
            elif retriever.error:
                ctx["fallback"], ctx["fallback_reason"] = True, f"KB retrieve failed: {retriever.error}"
            else:
                facts, skipped = parse_results(retriever.results or [])
            record.update(retriever.phases)
        # components: facts first (rank order), then names and glossary terms in the prompt
        tq = clock()
        matched: dict[str, list[str]] = {}
        if matcher:
            # order: each fact's first component (rank order), exact names in the prompt, the facts' other
            # components, then glossary terms (broadest last)
            cited = [[(node_id, f"cited by {f['fact_id']}") for name in f["components"]
                      for node_id in matcher.component(name)] for f in facts]
            ordered = [c[0] for c in cited if c]
            ordered += [hit for hit in prompt_hits if not hit[1].startswith("glossary")]
            ordered += [hit for c in cited for hit in c[1:]]
            ordered += [hit for hit in prompt_hits if hit[1].startswith("glossary")]
            for node_id, how in ordered:
                matched.setdefault(node_id, []).append(how)
        comps = []
        for node_id, how in list(matched.items())[:MAX_COMPONENTS]:
            try:
                comp = neighbourhood(graph, matcher, node_id, hops)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - one bad node never drops the rest
                continue
            comp["matched"] = list(dict.fromkeys(how))
            comps.append(comp)
        graph_ms += round((clock() - tq) * 1000)
        # outputs
        latest = out_dir / LATEST_FILE
        full_text = render_full(ctx, facts, comps)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with open(latest, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(full_text)
            record["latest_bytes"] = len(full_text.encode("utf-8"))
        except OSError as exc:
            record["write_error"] = f"{type(exc).__name__}: {exc}"
        commit = (ctx.get("commit") or "unknown")[:12]
        stale = sum(1 for f in facts if fact_freshness(f, ctx.get("commit")) == "verified-stale")
        header = (f"## Retrieved context (bcp kb hook): {len(facts)} verified KB facts"
                  + (f" ({stale} stale)" if stale else "")
                  + f", {len(comps)} components, code graph {commit}")
        footer = []
        if ctx["fallback"]:
            footer.append(f"- NOTE: unverified (graph-only fallback): {ctx['fallback_reason']}. No verified facts "
                          "retrieved; use the context files and the code.")
        elif not facts:
            footer.append("- NOTE: unverified: no verified fact covered this prompt; use the context files and "
                          "the code.")
        if ctx.get("graph_error"):
            footer.append("- NOTE: code graph unavailable; facts only.")
        footer.append(f"- Full results (fact text, evidence, all neighbours): {latest}")
        footer.append("- Facts are a map; the code is the truth; the mandatory code check still applies. "
                      "Cite [verified KB <fact-id>] and [code graph <commit>].")
        summary = fit_summary(header, summary_lines(facts, comps, ctx.get("commit")), footer)
        record.update({"fallback": ctx["fallback"], "fallback_reason": ctx["fallback_reason"],
                       "kb_ms": ctx["kb_ms"], "graph_ms": graph_ms, "facts": len(facts),
                       "facts_skipped_status": skipped, "fact_ids": [f["fact_id"] for f in facts],
                       "components": [c["id"] for c in comps], "graph_commit": ctx.get("commit"),
                       "graph_error": ctx.get("graph_error")})
    except Exception as exc:  # noqa: BLE001 - the hook must never fail the session
        summary = (f"## Retrieved context (bcp kb hook): unavailable ({type(exc).__name__}). "
                   "Continue with the context files and the code.")
        record.update({"fallback": True, "fallback_reason": f"hook error: {type(exc).__name__}: {str(exc)[:200]}"})
    record["summary_bytes"] = len(summary.encode("utf-8"))
    record["total_ms"] = round((clock() - t0) * 1000)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / TIMING_FILE, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except OSError:
        pass
    return summary, record, retriever


def build_parser() -> argparse.ArgumentParser:
    """Flags for script use (``bcp kb hook`` defines the same ones)."""
    parser = argparse.ArgumentParser(prog="bcp kb hook", description=__doc__.split("\n", 1)[0])
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Optional overrides of the settings file."""
    parser.add_argument("--settings", help=f"settings file (default <cwd>/{SETTINGS_REL})")
    parser.add_argument("--kb-id")
    parser.add_argument("--region")
    parser.add_argument("--graph", help="graph.json (relative to the workspace)")
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--timeout", type=float, dest="timeout_s", help="KB budget in seconds (default 12)")
    parser.add_argument("--hops", type=int, help="graph hops, 1..3 (default 2)")
    parser.add_argument("--endpoint-url", help="Bedrock agent runtime endpoint override (tests, fallback drills)")
    parser.add_argument("--cwd", help="workspace folder (default: the event's cwd)")


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """Override mapping from parsed flags."""
    return {k: getattr(args, k, None) for k in ("settings", "kb_id", "region", "graph", "top_k", "timeout_s",
                                                "hops", "endpoint_url", "cwd")}


def read_stdin() -> str:
    """The event JSON as UTF-8 (BOM tolerated), whatever the console code page."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8-sig", errors="replace")


def emit(summary: str) -> None:
    """Write the summary to stdout as UTF-8 and flush."""
    try:
        out = sys.stdout.buffer
        out.write(summary.encode("utf-8") + b"\n")
        out.flush()
    except Exception:  # noqa: BLE001
        try:
            print(summary, flush=True)
        except Exception:  # noqa: BLE001
            pass


def main(argv: Sequence[str] | None = None, hard_exit: bool = False) -> int:
    """Read stdin, run, print; always 0. ``hard_exit`` ends the process at once (script use) so an
    abandoned KB call cannot hold it open."""
    retriever = None
    try:
        args, _ = build_parser().parse_known_args(argv)
        stdin_text = "" if sys.stdin is None or sys.stdin.isatty() else read_stdin()
        summary, _, retriever = run_hook(stdin_text, overrides_from(args))
    except BaseException as exc:  # noqa: BLE001 - argparse SystemExit included
        summary = f"## Retrieved context (bcp kb hook): unavailable ({type(exc).__name__})."
    emit(summary)
    if hard_exit:
        try:
            sys.stderr.flush()
        finally:
            os._exit(0)
    return 0


if __name__ == "__main__":
    main(hard_exit=True)
