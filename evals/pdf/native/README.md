# 原生 PDF 冻结产物

这里保存两轮完整、公开的 MinerU 解析产物，不保存原 PDF 二进制或私有服务配置。
原始 JSON 字节不改写；每轮 manifest.json 锁定文件 SHA256 与规范正文/物理页身份。
校验和证明一致性，不认证远端身份、权重或人工独立性。两轮不能互换字符区间。

| 目录 | 来源 | canonical SHA256 |
|---|---|---|
| [20261025-b29](20261025-b29/manifest.json) | 第二十九批全量集成的完整7/80页 | `4048da0485907f0b23892cb4c7e8758c704d51dff490777045dbb0bede567651` |
| [20261025-b30](20261025-b30/manifest.json) | 第三十批全量集成的完整7/80页；新盲审默认输入 | `a5ef5b260b3472d8a8e0dc5541c8de24053bbd42cc70f0f06e0e643f02f7a1d8` |

产物包含OCR/排版/脚注等已知误差，并非出版物的权威文本；保留这些误差，不为标签
匹配重排原文。原PDF字节锁与选定问题页仍由[来源锁](../pages.json)定义。

- Mell P, Grance T (2011), *The NIST Definition of Cloud Computing*, NIST SP 800-145,
  National Institute of Standards and Technology, https://doi.org/10.6028/NIST.SP.800-145.
- Jansen W, Grance T (2011), *Guidelines on Security and Privacy in Public Cloud Computing*,
  NIST SP 800-144, National Institute of Standards and Technology,
  https://doi.org/10.6028/NIST.SP.800-144.

Republished courtesy of the National Institute of Standards and Technology.

原文件的Authority声明和[官方Technical Series许可](https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications)
已复核；该许可不推及NIST网站上所有第三方作品。保留原作者、版本与归属；历史文本
仅用于解析/检索评测，不提供当前安全或合规建议。

使用 `python -m evals.pdf.artifacts review --out /new/path` 从默认轮次生成盲审包。
审阅者只能依据导出的12个选定物理页回答24题；其他75页不扩充可回答范围。
原文片段的start/end引用该轮完整规范正文，不复用pypdf旧区间。表头、数据行和跨页
答案分列片段并注明联合需求；盲审不提供旧相关度、分类或模型排名。填写姓名不能
证明独立复核，包默认pending，后续仍需独立审阅与裁决。
