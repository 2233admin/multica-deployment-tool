# Optional Multica Adapter

This package is an optional bridge between the independent `agent-control` / `agent-plugins` repositories and Multica. It is not required to deploy or run either project.

The bridge performs a one-way, build-time translation of static Skill files into Multica Private Plugin V1 ZIPs. It does not add a runtime dependency, synchronize databases, call the Multica server, or install tools on Agent devices.

从分发包根目录执行；`agent-plugins` 是旁边克隆的独立仓库：

```powershell
python .\adapters\agent-plugins-multica\agent_plugins_to_multica.py `
  .\agent-plugins\plugins\adaptive-problem-solving `
  --output-dir .\generated\adaptive-problem-solving `
  --archive .\generated\adaptive-problem-solving-0.2.11.zip
```

For executable `agent-control` tools, install them on the Agent device and expose them through a local CLI or MCP server. Keep credentials and lifecycle management on that device.
