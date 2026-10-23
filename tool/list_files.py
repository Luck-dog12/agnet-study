"""以有界 glob 枚举项目根目录内的普通文件。"""

from app.schemas import ListFileEntry, ListFilesArguments, ListFilesOutput
from tool.project_scan import scan_project_files


def list_files(arguments: ListFilesArguments) -> ListFilesOutput:
    """列出匹配文件；没有 ``**`` 的模式不会递归到任意深度。"""
    arguments = ListFilesArguments.model_validate(arguments.model_dump())
    scanned = scan_project_files(arguments.pattern)
    selected = scanned.files[:arguments.max_results]
    return ListFilesOutput(
        pattern=arguments.pattern,
        entries=[ListFileEntry(path=item.path, size=item.size) for item in selected],
        count=len(selected),
        truncated=scanned.truncated or len(scanned.files) > arguments.max_results,
    )
