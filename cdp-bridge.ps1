# AdsPower CDP Bridge for WSL2
# Run this in a PowerShell window on Windows (no admin needed after firewall rule).
#
# ONE-TIME SETUP (run once as admin, then never again):
#   New-NetFirewallRule -DisplayName "AdsPower CDP WSL2" -Direction Inbound `
#     -LocalPort 49152-65535 -Protocol TCP -Action Allow -Profile Any

# Auto-detect the WSL2 adapter IP
$wslIp = (Get-NetIPAddress -InterfaceAlias "vEthernet (WSL)" `
    -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
if (-not $wslIp) {
    $wslIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match "^172\." } |
        Select-Object -First 1).IPAddress
}
if (-not $wslIp) { Write-Error "Cannot detect WSL2 IP"; exit 1 }

Write-Host "AdsPower CDP Bridge started on $wslIp" -ForegroundColor Green
Write-Host "Watching for Chrome debug ports (49152+)..." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop.`n" -ForegroundColor DarkGray

$relays = @{}

function Start-Relay($ip, $port) {
    $ps = [PowerShell]::Create()
    [void]$ps.AddScript({
        param($ip, $port)
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse($ip), $port)
        try { $listener.Start() } catch { return }
        while ($true) {
            $client = $null
            try { $client = $listener.AcceptTcpClient() } catch { break }
            $fwd = New-Object Net.Sockets.TcpClient
            try { $fwd.Connect("127.0.0.1", $port) } catch { $client.Close(); continue }
            $cs = $client.GetStream(); $ts = $fwd.GetStream()
            [void][Threading.Tasks.Task]::Run([Action]{ try { $cs.CopyTo($ts) } catch {} })
            [void][Threading.Tasks.Task]::Run([Action]{ try { $ts.CopyTo($cs) } catch {} })
        }
    }).AddArgument($ip).AddArgument($port)
    $handle = $ps.BeginInvoke()
    return @{ PS = $ps; Handle = $handle }
}

try {
    while ($true) {
        $active = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -ge 49152 } |
            Select-Object -ExpandProperty LocalPort)

        foreach ($port in $active) {
            if (-not $relays.ContainsKey($port)) {
                $relay = Start-Relay $wslIp $port
                $relays[$port] = $relay
                Write-Host "[+] Port $port  →  $wslIp`:$port → 127.0.0.1:$port" -ForegroundColor Cyan
            }
        }

        foreach ($port in @($relays.Keys)) {
            if ($active -notcontains $port) {
                try { $relays[$port].PS.Stop(); $relays[$port].PS.Dispose() } catch {}
                $relays.Remove($port)
                Write-Host "[-] Port $port closed" -ForegroundColor Yellow
            }
        }

        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($r in $relays.Values) {
        try { $r.PS.Stop(); $r.PS.Dispose() } catch {}
    }
    Write-Host "Bridge stopped." -ForegroundColor DarkGray
}
