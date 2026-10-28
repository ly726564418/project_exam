# 实测记录索引

日期：2026-10-28。主机 Windows / WSL2 / x86_64，Docker Desktop 在 QEMU 架构仿真下运行 Linux arm64；不是 Mac 原生数据。后台容器限制 4 vCPU / 4 GiB、发生器 1 vCPU / 256 MiB，均禁止额外 swap。

每轮文件后缀 `receipt.json` 为官方发生器原始凭据，`summary.json` 为测量脚本输出，`verification.json` 为按原文、ACK 位图和数据库逐条校对结果。

| test_id | 条件与用途 |
| --- | --- |
| `native-smoke-1000` | Windows Python 原生开发服务；目标 1,000/s，15 秒，batch=100，workers=4。常规 ACK 10,100，查询 P95 41.53 ms。不能当作受限 ARM 环境结果。 |
| `arm64-bursty-5000` | 受限 ARM 仿真；目标 5,000/s，30 秒，batch=100，workers=4。常规 ACK 13,500，约 450/s；查询 P95 331.88 ms；采样内存最高 166.7 MiB。结束时 4 个响应中断，但接收端保留对应原文及对象。 |
| `arm64-saturation` | 尽力发送 40 秒，batch=200，workers=8；常规 ACK 65,600，最终有效对象 66,796。中途在同一容器安装测试依赖，仅作为饱和接入与恢复诊断，**不用于独立性能结论**。 |
| `arm64-saturation-clean` | 无额外依赖安装的独立复测；已有约 8.3 万对象。尽力发送 30 秒，batch=200，workers=8。常规 ACK 52,000，全部 260 批成功；约 1,733/s 为原文落盘确认速度。最终新增有效对象 52,796；停止发送后另用约 75 秒处理完积压。查询 P95 365.02 ms，采样内存最高 284.9 MiB。 |

`arm64-saturation-clean` 的树修订 27 发出时接近运行结束，发生器记录 1 次树发送错误、确认到修订 26；接收端已经保存并物化修订 27。不要把运行停止时的取消误报成“发生器全部树消息确认成功”。对象常规批次没有超时、重试或失败。

所有轮次的故障注入均按原文保存：重复包仅计一次；缺失/无效字段的对象不进入正常档案；注入批次中其他有效对象保留。额外有效注入对象、结束时响应未确认但已接收对象，都解释了数据库对象数可能超过官方 `acknowledged_objects`。校对脚本分别核对，不能只比较这两个总数。

`crash-recovery.json`：在已有 6 条 ACK 时 SIGKILL，18 条请求中断；重启并重放 24 条消息，最终 2,400 对象、无重复计数，原始字节全部一致。

`unit-tests-arm64.txt`：将当前源码与测试复制到容器临时目录，在 Linux arm64 / Python 3.12 内实际执行测试；临时测试依赖不进入生产 Dockerfile。

`browser-control.json` 与对应 receipt：通过浏览器实际填写并提交发生器表单，经容器内控制代理启动官方发生器，确认真实对象进入系统。此项同时验证 host.docker.internal 的双向接入配置。

### 复测同等容器

```bash
docker build --platform linux/arm64 -t atlas-observatory:exam .
docker run -d --name exam-atlas-arm64 --platform linux/arm64 \
  --cpus=4 --memory=4g --memory-swap=4g -p 8002:8000 \
  -v atlas-exam-test:/data atlas-observatory:exam
# 官方发生器按根 README 单独启动，占用 9001 端口。
python scripts/generator_run.py --rate 0 --seconds 30 --batch 200 --workers 8 \
  --id your-new-saturation-run --system http://127.0.0.1:8002 \
  --target http://host.docker.internal:8002/ingest --container exam-atlas-arm64
```

**接入吞吐与持续物化吞吐不同。** 饱和轮次展示短时突发缓冲和随后恢复，不意味着可持续按 1,733/s 提供最新查询。当前热点是单写入器的 JSON 展开、字段统计和 SQLite 更新；GIL 与单写入模型也限制多核利用。长期发送应逐步调速，以 `pending`、最老待办时间和磁盘占用不持续增长为准。没有将受限仿真结果外推成 Mac 性能，也没有宣称千万级或长稳测试已经完成。
