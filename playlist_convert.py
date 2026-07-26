#!/usr/bin/env python3
"""
M3U8 播放列表转换工具

功能：
1. 移除 #EXT-X-KEY 部分（移除加密信息）
2. 将媒体文件重命名为 seg_000000 格式（6位数，前补0）
3. 可选择输出到控制台或直接修改原文件

CLI 用法:
    python3 playlist_convert.py <input_m3u8> [选项]

API 用法（外部脚本）:
    from playlist_convert import process_m3u8_content

    # 读取 M3U8 内容
    with open('video.m3u8', 'r') as f:
        content = f.read()

    # 处理内容（不包含 I/O）
    lines, mapping = process_m3u8_content(content)

    # 自定义输出逻辑
    for old_name, new_name in [m.split(' -> ') for m in mapping]:
        os.rename(old_name, new_name)

    with open('converted.m3u8', 'w') as f:
        f.writelines(lines)
"""

import argparse
import os
import re
import sys
from typing import List, Tuple

__all__ = [
    "parse_m3u8",
    "remove_ext_x_key",
    "rename_segments",
    "process_m3u8_content",
]


def parse_m3u8(content: str) -> List[str]:
    """
    解析 M3U8 文件内容，返回所有非注释行
    """
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def remove_ext_x_key(lines: List[str]) -> List[str]:
    """
    移除所有 #EXT-X-KEY 行
    后续可以加入其他需要移除的部分 暂时移除key
    """
    return [line for line in lines if not line.startswith("#EXT-X-KEY")]


def rename_segments(lines: List[str], output_format: str = "seg_{:06d}") -> Tuple[List[str], List[str]]:
    """
    重命名媒体片段，返回新的内容和映射关系

    Args:
        lines: M3U8 内容行
        output_format: 输出格式，默认为 "seg_{:06d}"

    Returns:
        Tuple[新的内容, 映射列表]
    """
    new_lines = []
    segment_map = []
    segment_number = 1

    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            # 生成新的片段名
            new_name = output_format.format(segment_number)
            segment_map.append(f"{line} -> {new_name}")
            new_lines.append(new_name)
            segment_number += 1
        else:
            new_lines.append(line)

    return new_lines, segment_map


def process_m3u8_content(content: str, output_format: str = "seg_{:06d}") -> Tuple[List[str], List[str]]:
    """
    处理 M3U8 内容，返回转换后的内容和映射关系

    Args:
        content: M3U8 文件内容
        output_format: 输出格式，默认为 "seg_{:06d}"

    Returns:
        Tuple[转换后的内容行列表, 片段映射列表]
    """
    lines = content.splitlines(keepends=True)

    # 1. 移除 #EXT-X-KEY
    lines_without_key = remove_ext_x_key(lines)

    # 2. 重命名片段
    lines_renamed, segment_map = rename_segments(lines_without_key, output_format)

    return lines_renamed, segment_map


def process_m3u8_file(input_file: str, output_file: str = None,
                     preview: bool = False, in_place: bool = False,
                     output_format: str = "seg_{:06d}") -> None:
    """
    处理 M3U8 文件（CLI 专用，包含 I/O 逻辑）

    Args:
        input_file: 输入文件路径
        output_file: 输出文件路径（如果不指定且不是 in_place 模式，则输出到控制台）
        preview: 预览模式，只显示不修改
        in_place: 直接修改原文件
        output_format: 输出格式，默认为 "seg_{:06d}"
    """
    # 检查输入文件是否存在
    if not os.path.exists(input_file):
        print(f"错误：输入文件 '{input_file}' 不存在", file=sys.stderr)
        sys.exit(1)

    # 读取文件内容
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 处理内容
    lines_renamed, segment_map = process_m3u8_content(content, output_format)

    # 输出结果
    if preview:
        print("=== 预览模式 ===")
        print("\n--- 转换后的 M3U8 内容 ---")
        print("".join(lines_renamed))
        print("\n--- 片段映射 ---")
        for mapping in segment_map:
            print(mapping)
        return

    if in_place:
        output_file = input_file
        print(f"直接修改原文件: {input_file}")
    else:
        if output_file:
            print(f"输出到文件: {output_file}")
        else:
            print("=== 输出到控制台 ===")
            print("".join(lines_renamed))
            print("\n--- 片段映射 ---")
            for mapping in segment_map:
                print(mapping)
            return

    # 写入文件
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            #f.writelines(lines_renamed) #不会为你添加换行符
            for line in lines_renamed:
                f.write(line + os.linesep)
        print(f"处理完成: {output_file}")
        print("\n--- 片段映射 ---")
    except Exception as e:
        print(f"写入文件失败: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="M3U8 播放列表转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 预览转换结果
  python3 playlist_convert.py video.m3u8 --preview

  # 直接修改原文件
  python3 playlist_convert.py video.m3u8 --in-place

  # 输出到新文件
  python3 playlist_convert.py video.m3u8 --output converted.m3u8

  # 自定义输出格式
  python3 playlist_convert.py video.m3u8 --output converted.m3u8 --format "part_{:06d}.ts"
        """
    )

    parser.add_argument(
        "input_file",
        help="输入的 M3U8 文件路径"
    )

    parser.add_argument(
        "--output", "-o",
        help="输出的文件路径（如果不指定，--preview 时显示到控制台，否则也显示到控制台）"
    )

    parser.add_argument(
        "--preview", "-p",
        action="store_true",
        help="预览模式，只显示转换结果，不修改文件"
    )

    parser.add_argument(
        "--in-place", "-i",
        action="store_true",
        help="直接修改原文件（谨慎使用！）"
    )

    parser.add_argument(
        "--format", "-f",
        default="seg_{:06d}",
        help="输出格式，使用 {:06d} 作为占位符（默认: seg_{:06d}）"
    )

    # 检查参数冲突
    args = parser.parse_args()

    if args.preview and args.in_place:
        print("错误：--preview 和 --in-place 不能同时使用", file=sys.stderr)
        sys.exit(1)

    if args.preview and args.output:
        print("警告：--preview 模式下，--output 参数将被忽略")

    if args.in_place and args.output:
        print("警告：--in-place 模式下，--output 参数将被忽略")

    process_m3u8_file(
        input_file=args.input_file,
        output_file=args.output,
        preview=args.preview,
        in_place=args.in_place,
        output_format=args.format
    )


if __name__ == "__main__":
    main()