#!/usr/bin/env python3
"""Create an editable project manifest and versioned DaVinci handoff packages.

Uses only Python's standard library. A handoff checks local files, not media
decodability, resolution, frame rate, colour, or visual quality.
"""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import sys


PROJECT_DIRS = ("brief", "images", "raw", "processed", "audio", "resolve", "export")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".m4v", ".webm"}
FILE_CHECK_NOTE = (
    "ready 仅表示每个镜头都存在非空的 processed 视频文件。"
    "仅检查路径范围、文件类型、扩展名和文件大小；未检测解码、4K、30fps、"
    "色彩、音画同步或画质，也未根据 manifest 中的 status 判定完成。"
)


class ProjectError(Exception):
    """A user-correctable project or manifest error."""


def positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError(f"{label} 必须是正数。")
    if not math.isfinite(value) or value <= 0:
        raise ProjectError(f"{label} 必须是有限的正数。")
    return value


def inside_project(path, root, label):
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectError(f"{label} 必须位于项目目录内：{path}") from exc
    return resolved


def project_root(value, must_exist=False):
    root = Path(value).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ProjectError(f"项目路径不是目录：{root}")
    if must_exist and not root.is_dir():
        raise ProjectError(f"项目目录不存在：{root}")
    return root


def check_subdirectory(root, name):
    path = root / name
    inside_project(path, root, name)
    if os.path.lexists(path) and not path.is_dir():
        raise ProjectError(f"{name} 必须是目录：{path}")
    return path


def write_json_exclusive(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def init_project(args):
    title = args.title.strip()
    language = args.language.strip()
    if not title or not language:
        raise ProjectError("title 和 language 不能为空。")
    duration = positive_number(args.duration, "duration")
    if float(duration).is_integer():
        duration = int(duration)
    root = project_root(args.project)
    manifest_path = root / "manifest.json"
    if os.path.lexists(manifest_path):
        raise ProjectError(f"已存在 manifest，拒绝覆盖：{manifest_path}")
    directories = [check_subdirectory(root, name) for name in PROJECT_DIRS]
    width, height = (3840, 2160) if args.orientation == "landscape" else (2160, 3840)
    manifest = {
        "version": 1,
        "title": title,
        "orientation": args.orientation,
        "target": {"width": width, "height": height, "fps": 30},
        "duration_seconds": duration,
        "language": language,
        "master_prompt": "",
        "shots": [],
    }
    root.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        directory.mkdir(exist_ok=True)
    write_json_exclusive(manifest_path, manifest)
    print(f"已创建项目：{root}")
    print(f"manifest：{manifest_path}")
    print("shots 当前为空；填写镜头后执行 handoff。")
    return 0


def load_manifest(root):
    path = inside_project(root / "manifest.json", root, "manifest.json")
    if not path.is_file():
        raise ProjectError(f"manifest.json 不存在或不是文件：{path}")
    try:
        with path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectError(f"manifest.json 不是有效的 UTF-8 JSON：{exc}") from exc
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest.get("version") != 1
    ):
        raise ProjectError("manifest 必须是对象且 version 为 1。")
    orientation = manifest.get("orientation")
    if orientation not in {"landscape", "portrait"}:
        raise ProjectError("orientation 必须为 landscape 或 portrait。")
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ProjectError("target 必须包含 width、height、fps。")
    expected_size = (3840, 2160) if orientation == "landscape" else (2160, 3840)
    for key, expected in zip(("width", "height", "fps"), (*expected_size, 30)):
        value = target.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != expected:
            raise ProjectError(f"{orientation} 的 target.{key} 必须为 {expected}。")
    positive_number(manifest.get("duration_seconds"), "manifest.duration_seconds")
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ProjectError("无镜头：请先在 manifest.shots 中填写镜头，不能生成就绪交接包。")
    return manifest


def media_path(root, shot, key):
    value = shot.get(key, "")
    if not isinstance(value, str):
        raise ProjectError(f"{shot['shot_id']}.{key} 必须是路径字符串或空字符串。")
    if not value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    path = inside_project(candidate, root, f"{shot['shot_id']}.{key}")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ProjectError(f"{shot['shot_id']}.{key} 不是支持的视频扩展名：{path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ProjectError(f"{shot['shot_id']}.{key} 不是普通文件：{path}")
    if path.stat().st_size == 0:
        raise ProjectError(f"{shot['shot_id']}.{key} 视频文件为空：{path}")
    return path


def validated_shots(root, manifest):
    seen = set()
    validated = []
    for index, shot in enumerate(manifest["shots"], 1):
        if not isinstance(shot, dict):
            raise ProjectError(f"第 {index} 个镜头必须是对象。")
        shot_id = shot.get("shot_id")
        if (
            not isinstance(shot_id, str)
            or not re.fullmatch(r"S[0-9]{3,}", shot_id)
            or int(shot_id[1:]) <= 0
        ):
            raise ProjectError(f"第 {index} 个 shot_id 必须形如 S001、S002。")
        if shot_id in seen:
            raise ProjectError(f"shot_id 重复：{shot_id}")
        seen.add(shot_id)
        duration = positive_number(shot.get("duration_seconds"), f"{shot_id}.duration_seconds")
        voiceover = shot.get("voiceover", "")
        if not isinstance(voiceover, str):
            raise ProjectError(f"{shot_id}.voiceover 必须是字符串。")
        processed = media_path(root, shot, "processed_video")
        raw = media_path(root, shot, "raw_video")
        if processed:
            selected, reason = processed, None
            note = "processed 文件存在；仅通过文件检查，画质及媒体参数待验收"
        elif raw:
            selected, reason = raw, "needs_processing"
            note = "needs_processing：仅有原始素材，先处理再导入达芬奇"
        else:
            selected, reason = None, "missing_video"
            note = "missing_video：缺少本地视频素材"
        validated.append({
            "shot_id": shot_id,
            "duration_seconds": duration,
            "voiceover": voiceover,
            "source_path": str(selected) if selected else "",
            "reason": reason,
            "note": note,
        })
    # The editable array defines the cut order; retain identifiers when reordered.
    total = math.fsum(shot["duration_seconds"] for shot in validated)
    if not math.isfinite(total):
        raise ProjectError("镜头总时长超出可用范围。")
    return validated, total


def reserve_handoff_directory(resolve_dir):
    resolve_dir.mkdir(exist_ok=True)
    for version in range(1, 1000000):
        path = resolve_dir / f"handoff-{version:03d}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise ProjectError("handoff 版本目录已用尽。")


def seconds_text(value):
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def import_notes(target):
    return (
        "这是顺序剪辑表和本地素材交接包，不是已导入的 DaVinci Resolve 工程。\n\n"
        f"1. 新建项目并先将时间线设为 {target['width']} × {target['height']}、30fps；"
        "横屏目标为 UHD 3840 × 2160，竖屏目标为 2160 × 3840。\n"
        "2. 查看 edit-plan.csv：source_path 是已存在素材的完整本地绝对路径；"
        "优先导入 note 中标记为 processed 的文件。\n"
        "3. 标记 needs_processing 的 raw 素材需先做必要的超分、帧率处理或转码；"
        "完成后在 manifest 中填写 processed_video 并生成新的 handoff。"
        "missing_video 镜头需补齐素材。\n"
        "4. 按表中的时间段组装镜头；配音文字见 narration.txt。"
        "镜头时长是计划值，未检测源文件是否足够长。\n"
        "5. 在 DaVinci Resolve 中完成剪辑、调色、配音和最终导出。\n\n"
        + FILE_CHECK_NOTE + "\n"
        "普通缩放到 4K 不代表恢复原始细节；变成 30fps 不代表完成运动插帧。\n"
    )


def handoff_project(args):
    root = project_root(args.project, must_exist=True)
    manifest = load_manifest(root)
    shots, total = validated_shots(root, manifest)
    resolve_dir = check_subdirectory(root, "resolve")
    output = reserve_handoff_directory(resolve_dir)
    paths = {
        "edit_plan": str(output / "edit-plan.csv"),
        "narration": str(output / "narration.txt"),
        "import_notes": str(output / "import-notes.txt"),
        "handoff": str(output / "handoff.json"),
    }
    with Path(paths["edit_plan"]).open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow((
            "shot_id", "timeline_in_seconds", "timeline_out_seconds",
            "source_path", "voiceover", "note",
        ))
        elapsed = 0.0
        for shot in shots:
            end = elapsed + shot["duration_seconds"]
            writer.writerow((
                shot["shot_id"], seconds_text(elapsed), seconds_text(end),
                shot["source_path"], shot["voiceover"], shot["note"],
            ))
            elapsed = end
    with Path(paths["narration"]).open("x", encoding="utf-8") as stream:
        for shot in shots:
            stream.write(f"[{shot['shot_id']}]\n{shot['voiceover']}\n\n")
    with Path(paths["import_notes"]).open("x", encoding="utf-8") as stream:
        stream.write(import_notes(manifest["target"]))
    incomplete = [
        {"shot_id": shot["shot_id"], "reason": shot["reason"], "source_path": shot["source_path"]}
        for shot in shots if shot["reason"]
    ]
    notes = [FILE_CHECK_NOTE, "按 manifest.shots 数组顺序生成计划；未修改 manifest 或原始素材。"]
    if not math.isclose(total, manifest["duration_seconds"], rel_tol=0, abs_tol=0.000001):
        notes.append(
            f"镜头计划总时长 {seconds_text(total)} 秒，与项目预设 "
            f"{seconds_text(manifest['duration_seconds'])} 秒不同。"
        )
    package = {
        "target": manifest["target"],
        "duration": total,
        "requested_duration_seconds": manifest["duration_seconds"],
        "ready": not incomplete,
        "incomplete_shots": incomplete,
        "paths": paths,
        "notes": notes,
    }
    write_json_exclusive(Path(paths["handoff"]), package)
    print(f"已生成交接包：{output}")
    for label, path in paths.items():
        print(f"{label}：{path}")
    if incomplete:
        print("未就绪镜头：" + ", ".join(f"{item['shot_id']} ({item['reason']})" for item in incomplete))
    else:
        print("素材文件齐备；仅通过文件检查，尚未验收画质或导入达芬奇。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="创建项目目录和空镜头 manifest，不覆盖已有 manifest")
    init.add_argument("project", metavar="PROJECT")
    init.add_argument("--title", required=True, metavar="TITLE")
    init.add_argument("--orientation", choices=("landscape", "portrait"), default="landscape")
    init.add_argument("--duration", type=float, default=60, metavar="SECONDS")
    init.add_argument("--language", default="zh")
    init.set_defaults(run=init_project)
    handoff = commands.add_parser("handoff", help="生成新的 resolve/handoff-NNN 交接目录")
    handoff.add_argument("project", metavar="PROJECT")
    handoff.set_defaults(run=handoff_project)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ProjectError, OSError, ValueError, OverflowError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
