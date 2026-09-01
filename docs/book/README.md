# 技术解读与交接说明 · LaTeX 源

沿用《蜡质相变俘获利用海洋温差能的理论与技术研究》书稿的排版体例。

## 编译

```bash
./build.sh
```

脚本依次执行 xelatex、逐章 bibtex、xelatex 两遍。
`chapterbib` 要求对每一章的 aux 单独跑 bibtex，`build.sh` 已处理。

## 目录结构

与原书稿一致：`main.tex` 在根目录，章节与 sty/bst 在 `chapter/`，图片在 `Figure/`。

## 中文支持：条件加载

原书稿用 `ctexcap`。但交付环境未必装有 `texlive-lang-chinese`，
因此 `main.tex` 做了条件加载：

```latex
\IfFileExists{ctexcap.sty}{\usepackage{ctexcap}}{\usepackage{ctexshim}}
```

装有 ctex 的机器走 `ctexcap`，与原书稿完全一致；
未装的走 `chapter/ctexshim.sty`，用 fontspec 与 XeTeX 断行原语补齐
`\heiti`、`\zihao`、中文章节名等接口。

**两条路径下正文、公式、表格、交叉引用、参考文献一致**，
差别仅在标点压缩——ctex 会做全角标点挤压，兼容层不做。
**无论走哪条路径，main.tex 本身不需要修改。**

## 两个字体上的坑，改之前先看

**一、代码块不能用中文字体。** Noto CJK 系列会把 ASCII 连字符 U+002D
映射成 U+2011，肉眼一模一样，但**从 PDF 复制出来的命令是错的，
粘到终端会执行失败**。实测 DejaVu 系列干净。

代价是代码块里不能写中文注释（DejaVu 没有汉字字形，会**静默丢字且不报错**）。
因此本稿约定：**代码块只放命令，解释写在紧接着的正文里**。

**二、不要用 listings。** 它自己做词法分析、逐字节处理，
在 XeLaTeX 下会把中文直接吞掉，同样不报错。本稿用 fvextra
（`fancyvrb` 本身没有 `breaklines`，由 fvextra 提供）。

## gbt7714 的一个坑

`gbt7714.sty` 默认走 2015 版，会写死 `\bibstyle{gbt7714-numerical}`，
而随附的 bst 是 2005 版。传 `2005` 选项也没用——那是 legacy 接口，
会导致 aux 里出现两条 `\bibstyle`，bibtex 直接报错。

本稿各章的做法是在章末显式指定：

```latex
\bibliographystyle{gbt7714-2005}
\bibliography{refs}
```

## 验收清单

改完后逐项确认：

```bash
grep -c "Missing character" main.log      # 缺字，必须 0
grep -c "^! " main.log                     # 错误，必须 0
grep -c "Overfull \\\\hbox" main.log       # 版面溢出
pdftotext main.pdf - | grep python3        # 命令能否正确复制
```

当前状态：34 页，缺字 0，错误 0，溢出 0，18 条命令行连字符全部正确。
