# AdsPower CDP Bridge — netsh portproxy edition
# Requires: admin PowerShell window
# Run: & "\\wsl$\Ubuntu\home\rumy\Personal\zola-agent\cdp-bridge.ps1"
#
# Uses kernel-level netsh portproxy (bypasses Windows loopback isolation).
# Watches for Chrome debug ports on 127.0.0.1 and routes
# 172.22.0.1:PORT → 127.0.0.1:PORT so WSL2 can reach them.

# Auto-detect WSL2 adapter IP
$wslIp = (Get-NetIPAddress -InterfaceAlias "vEthernet (WSL)" `
    -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
if (-not $wslIp) {
    $wslIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match "^172\." } |
        Select-Object -First 1).IPAddress
}
if (-not $wslIp) { Write-Error "Cannot detect WSL2 IP"; exit 1 }

Write-Host "AdsPower CDP Bridge started on $wslIp (netsh portproxy)" -ForegroundColor Green
Write-Host "Watching for Chrome debug ports (49152+)..." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop.`n" -ForegroundColor DarkGray

$active = @{}

function Add-Route($port) {
    netsh interface portproxy add v4tov4 `
        listenaddress=$wslIp listenport=$port `
        connectaddress=127.0.0.1 connectport=$port | Out-Null
    Write-Host "[+] Port $port  →  $wslIp`:$port → 127.0.0.1:$port" -ForegroundColor Cyan
}

function Remove-Route($port) {
    netsh interface portproxy delete v4tov4 `
        listenaddress=$wslIp listenport=$port | Out-Null
    Write-Host "[-] Port $port removed" -ForegroundColor Yellow
}

try {
    while ($true) {
        $current = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -ge 49152 } |
            Select-Object -ExpandProperty LocalPort)

        foreach ($port in $current) {
            if (-not $active.ContainsKey($port)) {
                Add-Route $port
                $active[$port] = $true
            }
        }

        foreach ($port in @($active.Keys)) {
            if ($current -notcontains $port) {
                Remove-Route $port
                $active.Remove($port)
            }
        }

        Start-Sleep -Milliseconds 500
    }
} finally {
    Write-Host "`nCleaning up routes..." -ForegroundColor DarkGray
    foreach ($port in @($active.Keys)) {
        Remove-Route $port
    }
    Write-Host "Done." -ForegroundColor DarkGray
}
