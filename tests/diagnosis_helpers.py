import json
from pathlib import Path

from flight_agent.verification.diagnosis_contracts import DiagnosisProposal, DiagnosisReview
from flight_agent.verification.planner import VerificationPlanner
from tests.test_diagnosis_contracts import diagnosis_proposal


def diagnostic_project(root: Path) -> Path:
    root.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0",
        "name": "Synthetic diagnosis fixture, not flight data",
        "synthetic": True,
        "design": {
            "controller": {"id": "controller", "version": "1", "sha256": "a" * 64},
            "plant": {"id": "plant", "version": "1", "sha256": "b" * 64},
        },
        "conditions": {"case": "synthetic-bias"},
        "datasets": [
            {"id": name, "path": f"{name}.json", "kind": "time_series", "source": "synthetic"}
            for name in ("observed", "reference")
        ],
    }
    (root / "flight-diagnosis.json").write_text(json.dumps(manifest))
    for name, values in (
        ("observed", [1.0, 2.0, 3.0, 4.0, 5.0]),
        ("reference", [0.0, 1.0, 2.0, 3.0, 4.0]),
    ):
        (root / f"{name}.json").write_text(json.dumps({
            "schema_version": "1.0",
            "kind": "time_series",
            "time_unit": "s",
            "time": [0.0, 1.0, 2.0, 3.0, 4.0],
            "signals": {"pitch": {"unit": "deg", "values": values}},
        }))
    return root


class AdaptivePlanner(VerificationPlanner):
    def __init__(self, proposal=None, *, repeat=False, invalid_citation=False):
        super().__init__()
        self.initial = proposal or diagnosis_proposal()
        self.repeat = repeat
        self.invalid_citation = invalid_citation
        self.seen = []

    def propose_diagnosis(self, message, conversation, project, *, tools):
        return DiagnosisProposal.model_validate(self.initial)

    def review_diagnosis(self, task, *, tools):
        self.seen.append([run.model_dump() for run in task.analysis_runs])
        run_id = "f" * 32 if self.invalid_citation else task.analysis_runs[-1].id
        summary = {
            "problem": "Recorded pitch differs from the reference.",
            "localization": "The recorded pitch comparison contains a nonzero mean error.",
            "responsibility": "undetermined",
            "reason": "An offset does not distinguish model and control causes.",
            "hypothesis_ids": ["bias"],
            "evidence": [{"run_id": run_id, "metric": "mean_error"}],
            "recommendations": ["Obtain independently labeled observations before attribution."],
        }
        if len(task.rounds) == 0 or self.repeat:
            analysis = diagnosis_proposal()["analyses"][0]
            if not self.repeat:
                analysis["inputs"].update(start=2.0, end=4.0)
            return DiagnosisReview.model_validate({
                "action": "continue",
                "reason": "Compare the later window before drawing a conclusion.",
                "assessments": [
                    {
                        "hypothesis_id": "bias",
                        "status": "supported",
                        "reason": "A nonzero mean error is present in the recorded window.",
                        "evidence": [{"run_id": run_id, "metric": "mean_error"}],
                    },
                    {
                        "hypothesis_id": "transient",
                        "status": "unknown",
                        "reason": "The later window has not yet been evaluated.",
                    },
                ],
                "unknowns": ["Whether the mismatch persists in the later window."],
                "analyses": [analysis],
                "summary": summary,
            })
        return DiagnosisReview.model_validate({
            "action": "stop",
            "stop_reason": "evidence_sufficient",
            "reason": "Both recorded windows have the same bias; this is not causal proof.",
            "assessments": [
                {
                    "hypothesis_id": hypothesis,
                    "status": status,
                    "reason": "The recorded mean error is unchanged in the later window.",
                    "evidence": [
                        {"run_id": item.id, "metric": "mean_error"} for item in task.analysis_runs
                    ],
                }
                for hypothesis, status in (("bias", "supported"), ("transient", "refuted"))
            ],
            "unknowns": ["No real labeled flight-test dataset has been supplied."],
            "summary": summary,
        })


class EvolvingPlanner(AdaptivePlanner):
    def __init__(self, *, fail_second=False, revision_fault=None):
        super().__init__()
        self.fail_second = fail_second
        self.revision_fault = revision_fault

    def review_diagnosis(self, task, *, tools):
        data = super().review_diagnosis(task, tools=tools).model_dump()
        run_id = task.analysis_runs[-1].id
        data["summary"] = {
            "problem": "Recorded pitch differs from the reference.",
            "localization": "The recorded pitch comparison shows a persistent offset.",
            "responsibility": "undetermined",
            "reason": "A recorded offset alone cannot distinguish model and control causes.",
            "hypothesis_ids": ["bias"],
            "evidence": [{"run_id": run_id, "metric": "mean_error"}],
            "recommendations": ["Verify acquisition timing and signal scaling independently."],
        }
        data["assessments"].append({
            "hypothesis_id": "alignment", "status": "unknown",
            "reason": "No independently verified acquisition timing is available.",
        })
        data["unknowns"].append("Acquisition timing is not independently verified.")
        if not task.rounds:
            data["hypothesis_revision"] = {
                "hypotheses": [
                    {"id": "bias", "statement": "A persistent recorded pitch offset.",
                     "priority": "high"},
                    task.baseline.hypotheses[1].model_dump(),
                    {"id": "alignment", "statement": "Acquisition alignment may contribute.",
                     "priority": "low"},
                ],
                "reason": "A nonzero mean error warrants a separate alignment hypothesis.",
                "evidence": [{"run_id": run_id, "metric": "mean_error"}],
            }
            data["analyses"][0]["hypothesis_ids"].append("alignment")
            if self.revision_fault == "drop":
                data["hypothesis_revision"]["hypotheses"].pop(1)
            elif self.revision_fault == "citation":
                data["hypothesis_revision"]["evidence"][0]["run_id"] = "f" * 32
            elif self.revision_fault == "scope":
                data["analyses"][0]["capability"] = "write_candidate"
            elif self.revision_fault == "attribution":
                data["summary"]["responsibility"] = "control"
                data["summary"]["hypothesis_ids"] = ["alignment"]
        else:
            data["stop_reason"] = "missing_input"
            if self.fail_second:
                data["summary"]["evidence"][0]["run_id"] = "f" * 32
        return DiagnosisReview.model_validate(data)
