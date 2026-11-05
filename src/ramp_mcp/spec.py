"""Index over Ramp's bundled OpenAPI spec, used for endpoint discovery."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class Operation:
    method: str
    path: str
    summary: str
    description: str
    tag: str
    scopes: list[str]
    raw: dict[str, Any]

    @property
    def is_read(self) -> bool:
        return self.method == "GET"

    def brief(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "tag": self.tag,
            "scopes": self.scopes,
        }


class SpecIndex:
    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.operations: list[Operation] = []
        self._templates: list[tuple[re.Pattern[str], Operation]] = []
        for path, item in spec.get("paths", {}).items():
            shared_params = item.get("parameters", [])
            for method in HTTP_METHODS:
                op = item.get(method)
                if not op:
                    continue
                if shared_params:
                    op = {**op, "parameters": shared_params + op.get("parameters", [])}
                scopes: list[str] = []
                for sec in op.get("security") or []:
                    for vals in sec.values():
                        scopes.extend(vals)
                o = Operation(
                    method=method.upper(),
                    path=path,
                    summary=(op.get("summary") or "").strip(),
                    description=(op.get("description") or "").strip(),
                    tag=(op.get("tags") or [""])[0],
                    scopes=scopes,
                    raw=op,
                )
                self.operations.append(o)
                pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", r"[^/]+", re.escape(path)) + "/?$"
                self._templates.append((re.compile(pattern), o))

    # --------------------------------------------------------------- lookup
    def find(self, method: str, path: str) -> Operation | None:
        method = method.upper()
        path = path.split("?", 1)[0]
        if not path.startswith("/developer/v1"):
            path = "/developer/v1" + ("" if path.startswith("/") else "/") + path
        for op in self.operations:
            if op.method == method and op.path == path:
                return op
        for pattern, op in self._templates:
            if op.method == method and pattern.match(path):
                return op
        return None

    def search(self, query: str, method: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        scored: list[tuple[int, Operation]] = []
        for op in self.operations:
            if method and op.method != method.upper():
                continue
            hay_main = f"{op.path} {op.summary} {op.tag}".lower()
            hay_desc = op.description.lower()
            score = 0
            for t in terms:
                if t in hay_main:
                    score += 3
                elif t.rstrip("s") and t.rstrip("s") in hay_main:
                    score += 2
                elif t in hay_desc:
                    score += 1
                else:
                    score -= 1
            if not terms or score > 0:
                scored.append((score, op))
        scored.sort(key=lambda x: (-x[0], x[1].path, x[1].method))
        return [op.brief() for _, op in scored[:limit]]

    def tags(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for op in self.operations:
            out[op.tag] = out.get(op.tag, 0) + 1
        return dict(sorted(out.items()))

    # ------------------------------------------------------------- describe
    def resolve(self, node: Any, depth: int = 0, max_depth: int = 4, seen: frozenset[str] = frozenset()) -> Any:
        """Inline ``$ref``s up to ``max_depth`` levels, trimming noise."""
        if isinstance(node, list):
            return [self.resolve(n, depth, max_depth, seen) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if ref in seen or depth >= max_depth:
                return {"$ref": name}
            target: Any = self.spec
            for part in ref.lstrip("#/").split("/"):
                target = target.get(part, {})
            return self.resolve(target, depth + 1, max_depth, seen | {ref})
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in {"example", "examples", "x-readme", "externalDocs"}:
                continue
            out[k] = self.resolve(v, depth, max_depth, seen)
        return out

    def describe(self, op: Operation, max_depth: int = 4) -> dict[str, Any]:
        raw = op.raw
        params = []
        for p in raw.get("parameters", []):
            p = self.resolve(p, max_depth=max_depth)
            params.append(
                {
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": p.get("required", False),
                    "description": (p.get("description") or "")[:400],
                    "schema": p.get("schema"),
                }
            )
        body = None
        rb = raw.get("requestBody")
        if rb:
            rb = self.resolve(rb, max_depth=max_depth)
            content = rb.get("content", {})
            ctype = next(iter(content), None)
            if ctype:
                body = {"content_type": ctype, "required": rb.get("required", False), "schema": content[ctype].get("schema")}
        response = None
        for code in ("200", "201", "202"):
            r = raw.get("responses", {}).get(code)
            if r:
                r = self.resolve(r, max_depth=max_depth)
                schema = next(iter(r.get("content", {}).values()), {}).get("schema")
                response = {"status": code, "schema": schema}
                break
        return {
            **op.brief(),
            "description": op.description[:3000],
            "parameters": params,
            "request_body": body,
            "response": response,
        }

    def query_param_names(self, method: str, path: str) -> list[str]:
        op = self.find(method, path)
        if not op:
            return []
        names = []
        for p in op.raw.get("parameters", []):
            p = self.resolve(p, max_depth=1)
            if p.get("in") == "query" and p.get("name") not in {"start", "page_size"}:
                names.append(p["name"])
        return names


@lru_cache(maxsize=1)
def load_spec() -> SpecIndex:
    text = resources.files("ramp_mcp").joinpath("openapi.json").read_text(encoding="utf-8")
    return SpecIndex(json.loads(text))
