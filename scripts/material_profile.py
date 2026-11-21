"""Review plain-text material and save a confirmed local interview profile."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app.schemas.materials import Confirmation, MaterialInput, ProjectInput
from app.services.materials import (
    TEXT_LIMIT, confirm_material, load_material, preview_text, start_draft, validate_material,
)


def read_fields() -> MaterialInput:
    jd = input("目标 JD（可选）> ").strip() or None
    skills = [skill.strip() for skill in input("技能（用逗号分隔，可选）> ").replace("，", ",").split(",") if skill.strip()]
    count = input("项目数（0–8，留空为 0）> ").strip() or "0"
    if count not in "012345678" or len(count) != 1:
        raise ValueError("项目数须为 0 到 8 的整数")
    projects = []
    for number in range(int(count)):
        print(f"第 {number + 1} 个项目，课程作业或自学练习也可以")
        projects.append(ProjectInput(
            name=input("名称（可选）> ").strip() or None,
            action=input("你做了什么（必填）> "),
            background=input("背景（可选）> ").strip() or None,
            result=input("结果或验证方式（可选）> ").strip() or None,
            excerpt=input("补充技术细节（可选）> ").strip() or None,
        ))
    return MaterialInput(jd_text=jd, skills=skills, projects=projects)


def read_material() -> MaterialInput:
    while True:
        try:
            material = read_fields()
            validate_material(material)
            return material
        except ValidationError:
            print("材料字段格式或长度不符合要求，请重新填写。", file=sys.stderr)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)


def create_session(db: Path, text_file: Path | None) -> str | None:
    if text_file is not None:
        with text_file.open("rb") as stream:
            raw = stream.read(TEXT_LIMIT * 4 + 4)
        if len(raw) > TEXT_LIMIT * 4 + 3:
            raise ValueError("材料文件超过预览长度限制")
        print("\n本机预览\n" + preview_text(raw.decode("utf-8-sig")))
        del raw
    print("请填写你愿意保存的字段，确认之前只保存在本次进程内。")
    material = read_material()
    draft = start_draft(material, "text" if text_file is not None else "form")
    while True:
        print(json.dumps(material.model_dump(exclude_none=True, exclude_defaults=True), ensure_ascii=False, indent=2))
        choices = "m 确认混合练习 / k 确认知识专项 / e 重新填写 / q 取消" if material.projects else (
            "k 确认知识专项 / e 重新填写 / q 取消"
        )
        choice = input(f"{choices} > ").strip().lower()
        if choice == "q":
            return None
        if choice == "e":
            print("请按原项目顺序修改，新增项目放在最后。")
            material = read_material()
        elif choice == "k" or (choice == "m" and material.projects):
            confirmation = Confirmation(material=material, mode="mixed" if choice == "m" else "knowledge_only")
            return confirm_material(db, draft.id, uuid4().hex, confirmation, draft=draft)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "interviews.db")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="review and confirm in one local process")
    create.add_argument("--text-file", type=Path, help="optional UTF-8 plain text to review locally")
    show = actions.add_parser("show", help="read an already confirmed snapshot")
    show.add_argument("session_id")
    args = parser.parse_args()
    try:
        if args.action == "create":
            session_id = create_session(args.db, args.text_file)
            if session_id is None:
                print("已取消，未保存材料。")
                return 0
            result = {"session_id": session_id, "phase": "PLANNING", "revision": 1}
        else:
            result = load_material(args.db, args.session_id)
    except (UnicodeError, OSError, sqlite3.Error):
        print("无法读取文本或操作业务库，请检查 UTF-8 编码、文件路径和数据库状态。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("输入已结束。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
