"""把快照嵌进单文件 showcase.html。

    python3 build_showcase.py

浏览器在 file:// 下会拦掉对本地 JSON 的 fetch，所以快照必须内联进 HTML，
不能靠运行时去读 snapshot.json。内联之后这个文件双击就能打开，
不依赖后端、不依赖网络，拿去录视频或者插 U 盘带到答辩现场都行。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TPL = ROOT / "web" / "showcase.template.html"
SNAP = ROOT / "web" / "snapshot.json"
OUT = ROOT / "web" / "showcase.html"


def main() -> None:
    if not SNAP.exists():
        raise SystemExit("缺少 web/snapshot.json，先跑 python3 -m evalkit.snapshot")
    data = json.loads(SNAP.read_text(encoding="utf-8"))
    # </script> 出现在 JSON 里会提前闭合脚本标签，必须转义
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")) \
        .replace("</", "<\\/")
    engine = (ROOT / "web" / "engine.js").read_text(encoding="utf-8")
    html = (TPL.read_text(encoding="utf-8")
            .replace("__SNAPSHOT__", blob)
            .replace("__ENGINE__", engine))
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")
    print("双击即可打开，无需后端。后端在线时会自动切换为实时模式。")


if __name__ == "__main__":
    main()
