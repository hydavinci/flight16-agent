import hashlib
import re
from pathlib import Path

from .models import Evidence, RuleSet, validate_source_id


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
        if len(run) == 1:
            terms.add(run)
    return terms


class KnowledgeBase:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Knowledge directory does not exist: {root}")
        self.documents: dict[str, str] = {}
        for path in sorted(self.root.rglob("*.md")):
            if not path.resolve().is_relative_to(self.root):
                raise ValueError(f"Knowledge document points outside its directory: {path.name}")
            source_id = validate_source_id(path.relative_to(self.root).as_posix())
            self.documents[source_id] = path.read_text(encoding="utf-8")
        if not self.documents:
            raise ValueError("Knowledge directory contains no Markdown documents")

    def validate_sources(self, rules: RuleSet) -> None:
        for rule in rules.rules:
            if rule.source_id not in self.documents:
                raise ValueError(f"Missing rule source: {rule.source_id}")
            if rule.source_section not in self.documents[rule.source_id]:
                raise ValueError(f"Missing source section: {rule.source_id}#{rule.source_section}")

    def search(self, query: str, limit: int = 3) -> list[Evidence]:
        if not 1 <= limit <= 5:
            raise ValueError("Search limit must be between 1 and 5")
        query_terms = _terms(query)
        ranked: list[tuple[int, str, str]] = []
        for source_id, text in self.documents.items():
            score = len(query_terms & _terms(source_id + " " + text))
            if score:
                paragraphs = re.split(r"\n\s*\n", text)
                paragraphs.sort(key=lambda part: len(query_terms & _terms(part)), reverse=True)
                excerpt = "\n\n".join(paragraphs)[:3000]
                ranked.append((score, source_id, excerpt))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return [
            Evidence(
                source_id=source_id,
                content=excerpt,
                sha256=hashlib.sha256(self.documents[source_id].encode("utf-8")).hexdigest(),
            )
            for _, source_id, excerpt in ranked[:limit]
        ]
