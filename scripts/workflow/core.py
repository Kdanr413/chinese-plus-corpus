from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSONL row must be an object")
            yield value


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            count += 1
    return count


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fieldnames: Sequence[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(*parts: Any, length: int = 16) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def path_manifest(paths: Iterable[Path], root: Path) -> list[dict[str, Any]]:
    manifest = []
    for path in sorted({p.resolve() for p in paths if p.exists()}):
        if not path.is_file():
            continue
        try:
            display = str(path.relative_to(root))
        except ValueError:
            display = str(path)
        manifest.append(
            {
                "path": display,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return manifest


@dataclass
class Feedback:
    severity: str
    category: str
    message: str
    object_id: str = ""
    suggested_action: str = ""
    assignee_role: str = ""
    status: str = "open"
    feedback_id: str = ""

    def as_row(self, stage_code: str) -> dict[str, str]:
        feedback_id = self.feedback_id or stable_id(
            stage_code, self.category, self.object_id, self.message
        )
        return {
            "feedback_id": feedback_id,
            "stage_code": stage_code,
            "severity": self.severity,
            "category": self.category,
            "object_id": self.object_id,
            "message": self.message,
            "suggested_action": self.suggested_action,
            "assignee_role": self.assignee_role,
            "status": self.status,
            "reviewer": "",
            "review_notes": "",
            "resolved_at": "",
        }


@dataclass
class StageResult:
    code: str
    name: str
    status: str
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    inputs: list[Path] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[Feedback] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


class WorkflowContext:
    def __init__(self, project_root: Path, config_path: Path):
        self.project_root = project_root.resolve()
        self.config_path = config_path.resolve()
        self.config = read_json(self.config_path)
        workflow = self.config.get("workflow", {})
        self.version = str(workflow.get("corpus_version", "0.0.0-draft"))
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()

    def configured_path(self, key: str) -> Path:
        return self.resolve(self.config["paths"][key])

    @property
    def reports_root(self) -> Path:
        return self.configured_path("reports_root") / self.version

    @property
    def release_root(self) -> Path:
        return self.configured_path("releases_root") / self.version

    def report_dir(self, code: str) -> Path:
        return self.reports_root / code


FEEDBACK_FIELDS = [
    "feedback_id",
    "stage_code",
    "severity",
    "category",
    "object_id",
    "message",
    "suggested_action",
    "assignee_role",
    "status",
    "reviewer",
    "review_notes",
    "resolved_at",
]


def _merge_feedback(path: Path, generated: list[dict[str, str]]) -> None:
    existing = {row.get("feedback_id", ""): row for row in read_csv(path)}
    merged = []
    for row in generated:
        old = existing.get(row["feedback_id"])
        if old:
            for field in ("status", "reviewer", "review_notes", "resolved_at"):
                if old.get(field):
                    row[field] = old[field]
        merged.append(row)
    write_csv(path, merged, FEEDBACK_FIELDS)


def _ensure_signoff(path: Path, result: StageResult, ctx: WorkflowContext) -> None:
    if path.exists():
        return
    fields = [
        "corpus_version",
        "stage_code",
        "stage_name",
        "role",
        "reviewer",
        "decision",
        "reviewed_at",
        "notes",
    ]
    rows = [
        {
            "corpus_version": ctx.version,
            "stage_code": result.code,
            "stage_name": result.name,
            "role": role,
            "reviewer": "",
            "decision": "pending",
            "reviewed_at": "",
            "notes": "",
        }
        for role in ("executor", "quality_reviewer", "accountable_owner")
    ]
    write_csv(path, rows, fields)


def finalize_stage(ctx: WorkflowContext, result: StageResult) -> Path:
    stage_dir = ctx.report_dir(result.code)
    stage_dir.mkdir(parents=True, exist_ok=True)
    feedback_rows = [item.as_row(result.code) for item in result.feedback]
    generated_feedback = stage_dir / "feedback_queue.generated.csv"
    write_csv(generated_feedback, feedback_rows, FEEDBACK_FIELDS)
    _merge_feedback(stage_dir / "feedback_queue.csv", feedback_rows)
    _ensure_signoff(stage_dir / "signoff.csv", result, ctx)

    metrics = {
        "schema_version": "1.0",
        "corpus_version": ctx.version,
        "run_id": ctx.run_id,
        "generated_at": now_iso(),
        "stage_code": result.code,
        "stage_name": result.name,
        "status": result.status,
        "summary": result.summary,
        "metrics": result.metrics,
        "validations": result.validations,
        "decisions": result.decisions,
        "inputs": path_manifest(result.inputs, ctx.project_root),
        "outputs": path_manifest(result.outputs, ctx.project_root),
        "feedback_open": sum(1 for row in feedback_rows if row["status"] == "open"),
    }
    metrics_path = stage_dir / "stage_metrics.json"
    write_json(metrics_path, metrics)

    lines = [
        f"# {result.code} {result.name}阶段说明",
        "",
        f"- 语料库版本：`{ctx.version}`",
        f"- 运行编号：`{ctx.run_id}`",
        f"- 生成时间：{metrics['generated_at']}",
        f"- 阶段状态：`{result.status}`",
        f"- 摘要：{result.summary}",
        "",
        "## 加工选择与决策",
        "",
    ]
    lines.extend(f"- {decision}" for decision in result.decisions)
    if not result.decisions:
        lines.append("- 本阶段未记录额外决策。")
    lines.extend(["", "## 典型数据", ""])
    lines.extend(f"- `{key}`：{value}" for key, value in result.metrics.items())
    lines.extend(["", "## 自动验证", ""])
    for validation in result.validations:
        mark = "通过" if validation.get("passed") else "未通过"
        lines.append(f"- {mark}｜{validation.get('name')}：{validation.get('details', '')}")
    if not result.validations:
        lines.append("- 无自动验证记录。")
    lines.extend(
        [
            "",
            "## 人工审核与反馈",
            "",
            f"- 反馈队列：`{stage_dir / 'feedback_queue.csv'}`",
            f"- 责任签署：`{stage_dir / 'signoff.csv'}`",
            "- 审核人员填写上述文件；再次运行不会覆盖已有审核状态和备注。",
            "",
            "## 输入与输出",
            "",
            f"- 输入文件数：{len(metrics['inputs'])}",
            f"- 输出文件数：{len(metrics['outputs'])}",
            "",
            "机器可读指标见 `stage_metrics.json`。",
        ]
    )
    report_path = stage_dir / "stage_report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_python(script: Path, arguments: Sequence[str], cwd: Path) -> None:
    command = [sys.executable, str(script), *map(str, arguments)]
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): "
            + " ".join(map(str, command))
        )
