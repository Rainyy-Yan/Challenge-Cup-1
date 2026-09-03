# Demo 原始来源片段

本目录保存正式 Demo 实际使用的第三方来源片段，供离线复核和哈希校验。每个
`KB-xxx.md` 对应一条知识库记录，包含官方链接、精确定位、必要的原文短摘和
忠实转述。它们不是完整手册镜像；原资料版权仍归各发布机构所有。

片段的 SHA-256 登记在 `data/sources/index.json` 与
`data/demo_source_manifest.json`。修改正文后必须同步更新两处哈希并重新执行
`python -m unittest tests.test_demo_sources -v`。
