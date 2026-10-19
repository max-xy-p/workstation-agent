# 后台启动本机服务，令牌保留在当前终端环境中。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Run uv sync in the repository before starting the local service.'
}
if (-not $env:WORKSTATION_AGENT_WORKSPACE) {
    $env:WORKSTATION_AGENT_WORKSPACE = $repoRoot
}
if (-not $env:WORKSTATION_AGENT_SERVICE_PORT) {
    $env:WORKSTATION_AGENT_SERVICE_PORT = '8765'
}
if (-not $env:WORKSTATION_AGENT_SERVICE_TOKEN) {
    $env:WORKSTATION_AGENT_SERVICE_TOKEN = [guid]::NewGuid().ToString('N')
}
& $pythonPath -m workstation_agent doctor --service | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'Invalid local service configuration; check the doctor output.'
}

# 提前检查服务端口是否可用。
$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback, [int]$env:WORKSTATION_AGENT_SERVICE_PORT
)
try {
    $listener.Start()
} catch {
    throw 'The configured service port is already occupied.'
} finally {
    $listener.Stop()
}
$serviceProcess = Start-Process -FilePath $pythonPath -ArgumentList '-m', 'workstation_agent', 'serve' `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
$healthUrl = "http://127.0.0.1:$($env:WORKSTATION_AGENT_SERVICE_PORT)/health"
$timer = [System.Diagnostics.Stopwatch]::StartNew()
try {
    while ($timer.Elapsed.TotalSeconds -lt 20) {
        if ($serviceProcess.HasExited) {
            throw 'The local service exited before becoming ready.'
        }
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1 -NoProxy
            if ($health.status -eq 'ok') {
                Write-Host "Local MCP service ready; process ID: $($serviceProcess.Id)"
                Write-Host 'Agent clients launched from this terminal inherit the service credential.'
                return $serviceProcess
            }
        } catch [System.Net.Http.HttpRequestException] {
            # 服务尚未就绪，输出不含原始异常和凭据的诊断后继续等待。
            Write-Verbose 'Health check could not connect; retrying until readiness timeout.'
        } catch [System.Threading.Tasks.TaskCanceledException] {
            # 单次检查超时后继续重试，仍受总等待时限约束。
            Write-Verbose 'Health check timed out; retrying until readiness timeout.'
        }
        Start-Sleep -Milliseconds 150
    }
    throw 'Local service readiness timed out.'
} catch {
    if (-not $serviceProcess.HasExited) {
        $serviceProcess.Kill()
        $serviceProcess.WaitForExit()
    }
    throw
}
