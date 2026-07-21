# Wiki Schema

## Domain

湖南省政务服务事项知识库，覆盖个人服务、法人服务和“高效办成一件事”主题索引。

## Conventions

- 普通事项一项一页，位于 `entities/services/`。
- “一件事”主题位于 `entities/theme-services/`，详情缺失时必须标记 `answerable: false`。
- 所有实体页和概念页使用YAML frontmatter及双向Wiki链接。
- 由于页面规模超过200页，根 `index.md` 只列分区索引；每个实体页必须进入一个 `_meta/indexes/` 子索引。
- `raw/` 是不可手工修改的来源层；重新生成时由转换程序维护。
- 不推测官网JSON中不存在的材料、条件、时限单位、地址或联系方式。
- 普通事项正文可作为回答依据；`detail_status: unavailable` 的主题页只用于检索导航。
- 原始结构化JSON位于本知识库上级数据目录，各事项通过 `source_json` 字段追溯。

## Frontmatter

必填字段：`title`、`created`、`updated`、`type`、`tags`、`sources`。

普通事项还必须包含：`service_id`、`service_objects`、`matter_type`、`detail_status`、`official_url`、`source_json`。

## Tag Taxonomy

- `government-service`
- `personal-service`
- `legal-entity-service`
- `theme-service`
- `detail-complete`
- `detail-unavailable`
- `administrative-license`
- `other-administrative-power`
- `public-service`
- `administrative-confirmation`
- `administrative-reward`
- `administrative-benefit`
- `administrative-adjudication`
- `other-matter-type`

## Page Thresholds

- 每个正式普通事项均为中央实体，允许独立建页。
- 每个“一件事”搜索主题均建导航页，但不能充当具体办理指南。
- 超过200行的页面进入转换报告，后续按材料或法律依据拆页。

## Retrieval Policy

- `_meta/rag_catalog.jsonl` 是后续RAG建索引的机器可读清单。
- 优先检索 `answerable: true` 的普通事项；主题页命中后应提示用户进入官方页面核实。
- 回答具体材料、条件、时限时，必须引用普通事项页面，不得从主题标题推断。

## Update Policy

- 重新爬取后再次运行转换器，按 `service_id` 更新同一页面。
- 页面标题变化不会改变已记录的文件路径。
- 旧版生成页不会自动删除，而是在转换报告中列为 stale，避免误删人工内容。

最后更新：2026-07-21
