"""Bound upload quota checks without blocking the portal event loop."""
import os
import stat
from pathlib import Path

UPLOAD_PATHS={"api/upload","api/workflow/upload","api/topic-workflow/upload"}


def project_usage(root: Path, stop_after: int) -> int:
    """Count project files, stopping once the rejection threshold is exceeded.

    Directory links are not followed outside the project. Vanished files can be
    ignored; other I/O errors propagate so that accounting never silently fails.
    """
    total=0
    pending=[Path(root)]
    while pending:
        folder=pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():continue
                        if os.name=="nt" and getattr(entry.stat(follow_symlinks=False),"st_file_attributes",0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:continue
                        if entry.is_dir(follow_symlinks=False):pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total+=entry.stat(follow_symlinks=False).st_size
                            if total>stop_after:return total
                    except FileNotFoundError:continue
        except FileNotFoundError:continue
    return total


def quota_error(root: Path, incoming: int, limit: int) -> str:
    used=project_usage(root,max(0,limit-incoming))
    if used+incoming<=limit:return ""
    gib=1024**3
    return (f"项目存储额度不足：已统计占用至少 {used/gib:.2f} GiB，"
            f"本次上传 {incoming/gib:.3f} GiB，项目上限 {limit/gib:.2f} GiB。"
            "请管理员调整 PORTAL_PROJECT_QUOTA_GB 后重启平台。")
