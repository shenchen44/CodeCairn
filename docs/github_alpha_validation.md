# GitHub Alpha 人工验收

本清单只用于显式的真实 GitHub Alpha 验收。默认测试不得访问 GitHub；任何
线上 E2E 测试都必须先设置 `CODECAIRN_E2E_GITHUB=1`。CodeCairn 不会自动
修改、合并或关闭 PR。

## 前置条件

1. 在受控测试仓库的 base 分支提交并审阅 `codecairn-trust.toml`。确认
   repository、workflow path/ref、event、artifact name、issuer 和结果最大
   年龄均符合测试环境。
2. 配置 GitHub CLI 或 `CODECAIRN_GITHUB_TOKEN`，只授予测试所需的最小权限。
   不要把 Token、私钥或 Header 写入命令历史、日志、artifact 或仓库。
3. 当前 PR workflow 是无特权 Observation producer，不接收长期签名私钥。
   若尚未配置受支持的平台/独立 Attestation，导入结果应保持 captured；这是
   正确的安全行为。

## 验收步骤

1. 认证：调用 GitHub `/user`，记录 login 和凭证来源，不记录 Token。
2. 从测试分支创建 PR，记录 owner/repository、PR number、head SHA、base SHA、
   base ref 和 workflow path/ref。
3. 等待 `.github/workflows/codecairn-ci.yml` 完成。Fork PR 在没有 Secret 时
   也应完成无特权测试并上传 `codecairn-ci-result`。
4. 通过 Artifacts API 记录 artifact id、size、expired 和 digest；下载 artifact。
   确认本地流式下载计算的 SHA-256 与 API digest 一致。
5. 验证 Observation 与 Attestation：
   - 无 Attestation 或无 digest：必须 captured；
   - 有 Attestation：核对 issuer、有效期、run identity、PR identity、snapshot、
     command、output 和 coverage 的全部绑定字段。
6. 执行 `cairn ci import-github --run-id <RUN_ID> ...`。检查导出中的
   trust source、policy hash、artifact id/digest 和失败原因；captured 结果不得
   提高 Gate 或 Assurance。
7. 对 description、comment、check 分别执行 `--dry-run`，确认 owner/repository、
   head/base、base ref、patch fingerprint 和工作区状态全部通过，且没有写操作。
8. 分别显式发布 description、comment、check。重复执行两次，确认更新原记录，
   没有创建重复 comment/check。
9. 在测试副本中制造错误 base 和错误 head，确认发布返回
   `github_pr_base_mismatch`、`github_pr_head_mismatch` 或
   `change_proof_stale`，且远端没有写入。
10. 用 MockTransport 完成 rate-limit 上限测试；真实验收只记录 GitHub 返回的
    rate-limit headers，不主动耗尽配额。
11. 清理本次由 CodeCairn 创建的测试 description block、comment 和 check；
    关闭测试 PR。不要删除其他用户内容。

## 记录模板

- GitHub login（无 Token）：
- repository / PR：
- workflow run id / attempt：
- artifact id / digest：
- Attestation issuer / id（如有）：
- policy hash：
- import trusted / captured 与原因：
- dry-run 绑定结果：
- description/comment/check 幂等结果：
- 错误 head/base 拒绝结果：
- rate-limit 观察：
- 清理结果：

没有实际执行的在线步骤必须明确写“未执行”，不得记为通过。
