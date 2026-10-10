import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .knowledge import KnowledgeBase
from .matlab import MatlabError
from .models import (
    AnalysisResult,
    Assessment,
    Evidence,
    Parameters,
    RuleSet,
    validate_result_context,
)
from .rules import assess


class AnalysisRunner(Protocol):
    log: str

    def run(self, parameters: Parameters) -> AnalysisResult: ...


def _write_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class AnalysisSession:
    def __init__(
        self,
        project_root: Path,
        parameters: Parameters,
        runner: AnalysisRunner,
        *,
        source: bytes | None = None,
        entry: str = "analyze_demo",
    ):
        self.project_root = project_root.resolve()
        self.parameters = parameters
        self.runner = runner
        self.rules = RuleSet.model_validate_json(
            (self.project_root / "rules" / "demo.json").read_text(encoding="utf-8")
        )
        self.knowledge = KnowledgeBase(self.project_root / "knowledge")
        self.knowledge.validate_sources(self.rules)
        self.result: AnalysisResult | None = None
        self.assessment: Assessment | None = None
        self.evidence: dict[str, list[Evidence]] = {}
        self.error: str | None = None
        self._lock = threading.RLock()
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
        self.run_dir = self.project_root / "runs" / run_id
        self.run_dir.mkdir(parents=True)
        input_json = parameters.model_dump_json()
        if source is None:
            source = (self.project_root / "matlab" / "analyze_demo.m").read_bytes()
        self.metadata: dict[str, object] = {
            "run_id": run_id,
            "status": "created",
            "created_at": datetime.now(UTC).isoformat(),
            "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "matlab_entry": entry,
            "matlab_source_sha256": hashlib.sha256(source).hexdigest(),
            "rule_version": self.rules.version,
            "demo_only": True,
        }
        _write_json(self.run_dir / "input.json", parameters.model_dump())
        _write_json(self.run_dir / "rules.json", self.rules.model_dump())
        (self.run_dir / "matlab_source.m").write_bytes(source)
        _write_json(self.run_dir / "metadata.json", self.metadata)

    def analyze(self) -> dict[str, object]:
        with self._lock:
            if self.error is not None:
                raise MatlabError(self.error)
            if self.result is None:
                self.metadata["status"] = "running_matlab"
                _write_json(self.run_dir / "metadata.json", self.metadata)
                try:
                    result = self.runner.run(self.parameters)
                    validate_result_context(result, self.parameters)
                    _write_json(self.run_dir / "result.json", result.model_dump())
                    self.result = result
                    self.metadata.update(
                        status="analyzed",
                        matlab_version=result.matlab_version,
                        model_version=result.model_version,
                    )
                    _write_json(self.run_dir / "metadata.json", self.metadata)
                except (MatlabError, OSError, ValueError) as exc:
                    self.fail(str(exc))
                    raise
                finally:
                    (self.run_dir / "matlab.log").write_text(self.runner.log, encoding="utf-8")
            return self.result.model_dump()

    def check_requirements(self) -> dict[str, object]:
        with self._lock:
            if self.result is None:
                raise ValueError("Run MATLAB analysis before checking requirements")
            self.assessment = assess(self.result, self.rules)
            _write_json(self.run_dir / "assessment.json", self.assessment.model_dump())
            return self.assessment.model_dump()

    def search_knowledge(self, query: str, limit: int = 3) -> list[dict[str, object]]:
        with self._lock:
            hits = self.knowledge.search(query, limit)
            for item in hits:
                excerpts = self.evidence.setdefault(item.source_id, [])
                if item not in excerpts:
                    excerpts.append(item)
            _write_json(
                self.run_dir / "evidence.json",
                [item.model_dump() for excerpts in self.evidence.values() for item in excerpts],
            )
            return [item.model_dump() for item in hits]

    def fail(self, message: str) -> None:
        with self._lock:
            self.error = message
            self.metadata.update(status="failed", error=message)
            _write_json(self.run_dir / "metadata.json", self.metadata)
            (self.run_dir / "report.md").write_text(
                "# Analysis failed\n\n"
                "No completed agent report. Any saved assessment remains separate.\n\n"
                f"Error: {message}\n\nThis project is demonstration-only.\n",
                encoding="utf-8",
            )

    def complete(self, explanation: str) -> Path:
        with self._lock:
            if self.error is not None or self.assessment is None or self.result is None:
                raise ValueError("Cannot complete a failed or unassessed analysis")
            assessment = self.assessment
            lines = [
                "# MATLAB Agent 分析报告（仅软件演示）",
                "",
                "**本报告不是适航、放飞或真实飞行器合格结论。**",
                "",
                "## 程序判定（不由大模型修改）",
                "",
                f"- 判定状态：`{assessment.status}`",
                f"- 覆盖：已评估 {assessment.evaluated} / 适用 {assessment.applicable}"
                f" / 总要求 {assessment.total}",
                f"- 判据版本：`{assessment.rule_version}`",
                f"- 模型版本：`{self.result.model_version}`",
                f"- 运行 ID：`{self.metadata['run_id']}`",
                "",
                "| 要求 | 指标 | 实测值 | 判据 | 状态 | 依据 |",
                "|---|---|---|---|---|---|",
            ]
            for check in assessment.checks:
                value = (
                    "-" if check.value is None
                    else f"{check.value:.8g} {check.value_unit or '单位未知'}"
                )
                lines.append(
                    f"| {check.rule_id} | {check.metric} | {value} | "
                    f"{check.operator} {check.threshold:g} {check.unit} | {check.status} | "
                    f"{check.source_id} / {check.source_section} |"
                )
            lines.extend(["", "### 逐项说明", ""])
            lines.extend(f"- {check.rule_id}: {check.reason}" for check in assessment.checks)
            lines.extend(
                [
                    "",
                    "## 模型解释（未经工程人员审核）",
                    "",
                    "下面是模型生成文本，不能覆盖上面的程序判定；原因与建议须另行验证。",
                    "",
                    explanation,
                    "",
                    "## 实际检索证据",
                    "",
                ]
            )
            for source_id, excerpts in self.evidence.items():
                lines.extend(
                    [
                        f"### [KB:{source_id}]",
                        "",
                        f"原文 SHA-256: `{excerpts[0].sha256}`",
                        "",
                    ]
                )
                for evidence in excerpts:
                    lines.extend("> " + line for line in evidence.content.splitlines())
                    lines.append("")
            path = self.run_dir / "report.md"
            path.write_text("\n".join(lines), encoding="utf-8")
            self.metadata.update(
                status="completed",
                assessment_status=assessment.status,
                completed_at=datetime.now(UTC).isoformat(),
            )
            _write_json(self.run_dir / "metadata.json", self.metadata)
            return path
