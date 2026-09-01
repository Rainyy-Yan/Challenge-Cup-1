#!/usr/bin/env bash
# 编译。chapterbib 需要对每一章的 aux 单独跑 bibtex。
set -e
cd "$(dirname "$0")"
rm -f main.aux main.out main.toc main.lof main.lot chapter/*.aux
export TEXINPUTS="./chapter//:$TEXINPUTS"
export BIBINPUTS="./chapter//:$BIBINPUTS"
export BSTINPUTS="./chapter//:$BSTINPUTS"
xelatex -interaction=nonstopmode main.tex > /dev/null
for f in chapter/chapter*.aux chapter/appendix.aux; do
  [ -f "$f" ] && grep -q bibdata "$f" && bibtex "${f%.aux}" > /dev/null 2>&1 || true
done
xelatex -interaction=nonstopmode main.tex > /dev/null
xelatex -interaction=nonstopmode main.tex > /dev/null
echo "已生成 main.pdf（$(pdfinfo main.pdf 2>/dev/null | awk '/^Pages/{print $2}') 页）"
