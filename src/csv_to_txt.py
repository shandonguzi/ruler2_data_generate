"""把 CSV 原样另存为同目录 TXT，保留原编码和内容。"""

from pathlib import Path
import shutil
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python src/csv_to_txt.py <文件.csv>")
    source = Path(sys.argv[1])
    if not source.is_file() or source.suffix.lower() != ".csv":
        raise SystemExit("请输入存在的 CSV 文件路径")
    target = source.with_suffix(".txt")
    try:
        with source.open("rb") as input_file, target.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file)
    except FileExistsError:
        raise SystemExit(f"目标文件已存在，未覆盖：{target}") from None
    print(f"已生成：{target}")


if __name__ == "__main__":
    main()
