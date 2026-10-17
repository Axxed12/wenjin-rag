# GPT子Agent经MCP取证回答

日期：2026-10-17。生成者为新建的GPT-6 Luna high子Agent `delivery_answer_agent`，fork none，未向其提供预期答案或资料正文。它只收到临时服务地址和凭据描述文件位置；要求SDK客户端仅读bearer与query，不直接查看fixture源码、文件目录或其它metadata。

## 实际调用与结论

- 自然问题：根据岚湾档案管理指南，正式业务档案的默认保留期是多少？临时导出副本有什么不同？请引用证据。
- 通过实际SDK网络客户端完成initialize（2025-11-25）、list_tools，观察真实工具schemas；随后list_corpora(include_files=true)发现合成库`synthetic-lanwan-archive`、版本`2.3`。
- 实际search_docs参数：query为上述原题，library=`synthetic-lanwan-archive`，version=`2.3`，top_k=5，context_char_budget=8000。返回rank1、行1–7及原文下载路径。工具字段evidence_status为unverified、answer_status为not_generated，不将其写成系统自动完成来源证明。
- 生成者依工具返回路径，通过同一临时Bearer实际HTTP GET `/files/synthetic-lanwan-archive-2.3/records/retention-guide.txt`：200，text/plain，391字节，SHA256=`13f27219e639223f5ca01e72de4f9f6a8449b98585a94a52e03ed6350cab5097`。协调者在生成回答后只读fixture描述文件中的source_sha256，确认与该观察一致，未再发起重复业务调用。
- 生成者引用原文：已批准的业务档案默认保留三十六个月，自项目验收日期起算。临时导出副本应在九十天内清除；它不改变正式档案的保留期限。
- 最终回答：正式档案36个月、从项目验收日期起算；临时导出副本90天内清除，且不改变正式期限。对个人身份信息和法定诉讼保全资料，原文声明不适用，不能据此编造期限。

## 适用范围

这证明真实GPT子Agent通过SDK调用当前隔离MCP并根据取回的原文生成回答，不需要额外部署生成模型API。它不是Claude Code/OpenCode本身的生成验收，不是公司资料、公司8B模型或跨机器验证。检索使用合成模型/索引；生成者能引用字面事实不等于独立语义质量评测。回答属于待人工审阅候选，不是黄金答案。源服务与凭据由独立测试Agent在CLI检查后清理，清理状态另见测试记录；本文件不含凭据。
