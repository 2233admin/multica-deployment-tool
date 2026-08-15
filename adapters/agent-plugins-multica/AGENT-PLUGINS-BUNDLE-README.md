# agent-plugins → Multica V1 预转换包

这个 ZIP 包含 `zaurakworks/agent-plugins` 当前 7 个 Plugin 的 Multica Private Plugin V1 归档。每个内部 ZIP 都是独立安装单元：

```powershell
multica plugin validate .\adaptive-problem-solving.zip
multica plugin install .\adaptive-problem-solving.zip --workspace YOUR_WORKSPACE
```

先在 NAS 启用 `FF_PLUGINS_V1=true` 和 `FF_PRIVATE_PLUGINS_V1=true`，再在 Multica CLI 或 **Settings → Plugins** 中安装和启用。版本沿用源 Plugin 的版本；内容变化时必须重新生成并递增版本，不要覆盖同一版本。

包内只包含 `skills/` 下的静态 UTF-8 Skill 及其 references/agents 文件，不包含源仓库根目录的脚本、测试、provider manifest 或任意安装 Hook。`agent-control` 的 Python/Go 工具不在这个 ZIP 中，应安装到实际运行 Agent 的设备并通过本地 runtime/MCP 调用。
