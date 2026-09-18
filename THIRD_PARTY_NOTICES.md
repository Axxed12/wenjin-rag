# 第三方资料与许可说明

本项目代码以根`LICENSE`（MIT）授权。但下列第三方资料是**独立授权**的检索实验语料，
不因进入本仓库而变为项目自身代码，也不改变其原始许可。

公开仓库**不重新分发这些资料的原始文件**：`raw_data/`被Git忽略，仓库内只保留
`corpora/<corpus-id>/source-manifest.json`（上游URL、固定版本/commit、逐文件SHA256、
许可与截止日期），需要原文时用该清单重新获取并逐字节校验。

## OpenSceneGraph 3.6.5

- 上游：https://github.com/openscenegraph/OpenSceneGraph
- 固定提交：`a827840baf0786d72e11ac16d5338a4ee25779db`
- 文件清单及逐文件来源：`corpora/osg-3.6.5/source-manifest.json`
- 原始许可：manifest下载后位于本机`raw_data/osg/3.6.5/LICENSE.txt`；公开仓库不重新分发原文件
- 范围：选定头文件、实现、示例、README和许可证；不是完整可编译SDK。

文件中的原始版权头和许可证必须保留。获取脚本按固定commit下载，并在落盘前核对manifest中的
SHA256。

## osgEarth 3.8

- 上游：https://github.com/pelicanmapping/osgearth
- 固定commit：`85d84d3e464a8400a43a77b61fc6f7d9115eb189`；发布日期2026-03-04
- 许可：**LGPL-3.0（含osgEarth附加例外）**
- 范围：15个文件入清单，13个进入精确检索白名单；不重新分发原文件。
- 注意：LGPL不因本项目只做检索而改变；如将来要重新分发osgEarth源码或衍生作品，
  须按LGPL-3.0及其例外条款单独评估。

## GDAL 3.13.1

- 上游：https://gdal.org/
- 固定commit：`b2e6057d1d0f2cb4c11bfdf79ab1a61def0ce9ca`
- 许可：MIT（GDAL/OGR，X/MIT风格）；**部分组件另有许可**，以`LICENSE.TXT`为准
- 范围：17个文件入清单，14个进入精确检索白名单；只归档网页文档，不重新分发原文件。

## PROJ 9.8.1

- 上游：https://proj.org/
- 固定commit：`f08fa86c478c4bbbf003b1ec751dd84aa6eca486`
- 许可：MIT（X/MIT风格）
- 范围：12个文件入清单，9个进入精确检索白名单（含README、2份RST和7份man手册）。

## OGC 3D Tiles 1.1 与 OGC WMS 1.3.0

- 上游：https://www.ogc.org/standards/3dtiles/ 和 https://www.ogc.org/standards/wms/
- 版本：3D Tiles 1.1（2023-01-12）；WMS 1.3.0（OGC 06-042，2006-03-15）
- 许可：**OGC Document Notice / OGC License**（https://www.ogc.org/license）
- 范围：各2个文件进入精确检索白名单（规范HTML、PDF和XSD）；不重新分发原文件。
- 注意：OGC规范文本保留其版权声明；本仓库不转载规范正文，也不对OGC规范作任何改动或
  再授权声明。本机用于验收的规范页截图与XSD副本已加入`.gitignore`，不进公开仓库。

## 其他不进公开仓库的第三方原文副本

`artifacts/corpus-acceptance-001/`下曾有OGC规范页截图（`wms-page-3.png`）、OGC WMS XSD
（`wms-upstream.xsd`）、OGC许可页HTML和PROJ文档摘录（`proj-*-pinned.rst`）。这些文件是本机
验收证据，自2026-09-18起加入`.gitignore`；仓库只保留引用它们的JSON汇总和来源URL。

## pdfplumber解析实验样本

- 上游：https://github.com/jsvine/pdfplumber
- 固定revision：`4c64b92d5caccd71c645e98e0fabb0c4dba7ff45`
- 样本来源与哈希：`data/packages/parse-lab/manifest.json`
- 用途：解析教学和对照实验，不进入正式OSG检索索引。

`scan.pdf`、`page-image.png`及解析预览是本地可重建衍生件，默认不提交Git。

## 本地模型与软件镜像

Qwen3 Embedding/Reranker模型、MinerU、Qdrant、Python包和Docker基础镜像不作为仓库代码重新
授权。部署者应分别遵守各自许可证。模型目录和Docker离线tar不提交Git。
