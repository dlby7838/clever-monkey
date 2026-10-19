# 后续测试调用政策

用户已指定后续测试以批量处理、低优档位节省成本。

- P1 CLI 和5条评测入口默认使用 Chat API + `service_tier=flex`，关闭思考。多个素材在本地任务循环中批量处理，当前并发1。
- 这是“批量任务走低优接口”，不是旧的 `/batch/chat/completions`。官方已将旧批量接口标为待下线，新业务建议使用 Chat API + flex。批量价与低优价不能叠加成四分之一。
- 可显式选择 `--service-tier default` 做常规在线对照。未明确改变选项时不使用常规档；不支持flex、未开通或接口拒绝时保留错误，不自动切回更贵档位。若服务端返回不同档位，立即停止后续请求并如实记录已发生费用。
- 价格计算按实际档位区分：已核对的公开低优非缓存输入/输出约为常规50%，缓存命中单价保持原价，不能把全请求费用盲目除2。具体版本和账号是否支持须以接口返回为准。当前没有新增flex真实调用，也不宣称已完成flex可用性验证。
- CLI等待超时默认600秒，无自动重试；服务端等待头受官方上限约束。超时可能已经计费，不自动重发。
- 缓存键区分服务档位与关闭思考策略；旧常规结果不冒充flex实测。续跑不允许中途改变档位，以免混淆费用和对照。
- 保持NAS只读，使用本地测试集，临时压缩副本完成后清理，100条测试原片保留。

参考：
- https://docs.volcengine.com/docs/ark/batch-inference
- https://docs.volcengine.com/docs/ark/online-inference-flex
- https://docs.volcengine.com/docs/ark/model-pricing

2026-10-19原始5条常规试测的明细：本地data/evaluations/oatmeal-pilot5-20261019/MODEL_OUTPUTS.md。历史报告保留原始default设置与计费结果，不追溯改写为flex或折扣成本。
