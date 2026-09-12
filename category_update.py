#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_toc.py —— 自动更新 Markdown 文档目录

用法：
    python update_toc.py                  # 更新根目录 README.md 里的目录
    python update_toc.py --stdout         # 只打印，不改文件
    python update_toc.py -o SUMMARY.md    # 写到 SUMMARY.md
    python update_toc.py -r docs          # 只扫描 docs 子目录
    python update_toc.py --exclude tmp,bak,old
    python update_toc.py --filename       # 用文件名代替文档一级标题

说明：
    * 只扫描 .md / .markdown 文件，其它一律忽略
    * 自动跳过 assets / assert / static / node_modules 等资源与构建目录
    * 自动跳过隐藏文件、隐藏目录
    * 只替换 <!-- TOC --> 与 <!-- /TOC --> 之间的内容，其余原样保留
    * 若文件里没有这对标记，会插入到第一个标题之后（没有标题则追加到末尾）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# ============================ 配置区（按需修改） ============================

# 需要排除的目录名（只匹配目录名，不区分大小写）
EXCLUDE_DIRS = {
    # 资源 / 附件目录（含你提到的 assert，以及常见的 assets 写法）
    "assets", "asset", "assert",
    "static", "public", "images", "image", "img",
    "media", "files", "res", "resources", "attachments",
    # 依赖 / 构建产物
    "node_modules", "dist", "build", "out", "target", "coverage",
    "__pycache__", ".venv", "venv", "env",
    # 版本控制 / 编辑器
    ".git", ".github", ".gitee", ".svn", ".vscode", ".idea", ".husky",
}

# 需要排除的文件名（小写比较）
EXCLUDE_FILES = {"summary.md"}

# 参与扫描的扩展名
MD_SUFFIXES = {".md", ".markdown"}

# 目录标记
TOC_START = "<!-- TOC -->"
TOC_END = "<!-- /TOC -->"

# ==========================================================================

TITLE_RE = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
TOC_RE = re.compile(re.escape(TOC_START) + r".*?" + re.escape(TOC_END), re.DOTALL)
FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ------------------------------- 工具函数 -------------------------------

def read_text(path: Path) -> str:
    """读取文本，容忍 UTF-8 / GBK 编码。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_title(path: Path) -> str:
    """取文档第一个标题作为目录项文字，取不到就用文件名。"""
    try:
        text = read_text(path)
    except OSError:
        return path.stem

    in_code = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = TITLE_RE.match(line)
        if m:
            return m.group(1).strip()
    return path.stem


def escape_label(text: str) -> str:
    """转义 Markdown 链接文字里的特殊字符。"""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def sorted_entries(directory: Path, exclude_dirs: set) -> list:
    """列出目录下需要处理的条目：目录在前、文件在后，各自按名称排序。"""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    result = []
    for p in entries:
        if p.name.startswith("."):          # 隐藏文件 / 目录
            continue
        try:
            if p.is_dir():
                if p.name.lower() in exclude_dirs:
                    continue
            elif p.is_file():
                if p.name.lower() in EXCLUDE_FILES:
                    continue
                if p.suffix.lower() not in MD_SUFFIXES:   # 排除非 md 文件
                    continue
            else:
                continue
        except OSError:
            continue
        result.append(p)

    result.sort(key=lambda x: (x.is_file(), x.name.lower()))
    return result


# ------------------------------- 目录生成 -------------------------------

def build_toc(directory: Path, output_dir: Path, exclude_dirs: set,
              skip_paths: set, use_title: bool,
              depth: int = 0, max_depth: int = 0):
    """递归生成目录行，返回 (行列表, 文档数)。"""
    lines = []
    count = 0
    indent = "  " * depth

    for p in sorted_entries(directory, exclude_dirs):
        if p in skip_paths:                 # 跳过输出文件自身
            continue

        if p.is_dir():
            sub_lines, sub_count = [], 0
            if not (max_depth and depth + 1 >= max_depth):
                sub_lines, sub_count = build_toc(
                    p, output_dir, exclude_dirs, skip_paths,
                    use_title, depth + 1, max_depth,
                )
            if sub_lines:                    # 目录里没有文档就不显示
                lines.append(f"{indent}- **{p.name}/**")
                lines.extend(sub_lines)
                count += sub_count
        else:
            rel = os.path.relpath(p, output_dir).replace(os.sep, "/")
            rel_url = rel.replace(" ", "%20")
            label = read_title(p) if use_title else p.stem
            lines.append(f"{indent}- [{escape_label(label)}]({rel_url})")
            count += 1

    return lines, count


# ------------------------------- 写回文件 -------------------------------

def update_file(path: Path, block: str) -> bool:
    """把目录块写进文件，返回是否发生了改动。"""
    old = read_text(path) if path.exists() else ""

    m = TOC_RE.search(old)
    if m:
        new = old[:m.start()] + block + old[m.end():]
    elif not old.strip():
        new = block + "\n"
    else:
        # 没有标记：插到第一个标题之后，否则追加到末尾
        lines = old.splitlines()
        insert_at = None
        in_code = False
        for i, line in enumerate(lines):
            if FENCE_RE.match(line):
                in_code = not in_code
                continue
            if not in_code and TITLE_RE.match(line):
                insert_at = i + 1
                break

        if insert_at is None:
            new = old.rstrip("\n") + "\n\n" + block + "\n"
        else:
            lines[insert_at:insert_at] = ["", block]
            new = "\n".join(lines)
            if not new.endswith("\n"):
                new += "\n"

    if new == old:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    return True


# --------------------------------- 入口 ---------------------------------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="自动更新 Markdown 文档目录（TOC）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-r", "--root", default=".",
                        help="扫描起始目录，默认脚本所在目录")
    parser.add_argument("-o", "--output", default="README.md",
                        help="写入的目标文件，默认 README.md")
    parser.add_argument("--stdout", action="store_true",
                        help="只打印目录，不修改任何文件")
    parser.add_argument("--filename", action="store_true",
                        help="使用文件名而不是文档一级标题作为目录项")
    parser.add_argument("--exclude", default="",
                        help="额外排除的目录名，逗号分隔，如 tmp,bak")
    parser.add_argument("--max-depth", type=int, default=0,
                        help="最大目录层级，0 表示不限制")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent

    root = Path(args.root)
    if not root.is_absolute():
        root = script_dir / root
    root = root.resolve()
    if not root.is_dir():
        print(f"[错误] 扫描目录不存在：{root}", file=sys.stderr)
        return 1

    output = Path(args.output)
    if not output.is_absolute():
        output = script_dir / output
    output = output.resolve()

    exclude_dirs = set(EXCLUDE_DIRS)
    exclude_dirs |= {d.strip().lower() for d in args.exclude.split(",") if d.strip()}

    lines, total = build_toc(
        directory=root,
        output_dir=output.parent,
        exclude_dirs=exclude_dirs,
        skip_paths={output},
        use_title=not args.filename,
        depth=0,
        max_depth=args.max_depth,
    )

    body = "\n".join(lines) if lines else "_暂无文档_"
    block = f"{TOC_START}\n\n{body}\n\n{TOC_END}"

    if args.stdout:
        print(block)
        return 0

    changed = update_file(output, block)
    print(f"[{'已更新' if changed else '无变化'}] {output}（{total} 篇文档）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
