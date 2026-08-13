# Sube el codigo al VPS por scp y reinicia el servicio.
#
#   $env:GARMIN_VPS = "root@TU_IP"
#   .\scripts\deploy.ps1
#
# El host NO va escrito aqui a proposito: este repo es publico. Sale de la
# variable de entorno GARMIN_VPS o del parametro -Vps.
#
# Nunca toca .env ni data/ del servidor: las credenciales y los tokens OAuth se
# suben a mano una sola vez y un despliegue jamas debe pisarlos.
param(
    [string]$Vps     = $env:GARMIN_VPS,
    [string]$Key     = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$Destino = "/root/garmin-ia",
    [string]$Servicio = "garmin-api"
)

$ErrorActionPreference = "Stop"

if (-not $Vps) {
    throw "Falta el host. Define `$env:GARMIN_VPS = 'root@TU_IP'` o pasa -Vps."
}

$sshArgs = @("-o", "StrictHostKeyChecking=no", "-i", $Key)
$raiz    = Split-Path $PSScriptRoot -Parent
$tmp     = Join-Path $env:TEMP "garmin-deploy"

if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }

Write-Host "Preparando copia limpia..."
robocopy (Join-Path $raiz "app") (Join-Path $tmp "app") /E /XD __pycache__ /XF "*.pyc" | Out-Null
# robocopy devuelve <8 en exito; 8 o mas es error de verdad.
if ($LASTEXITCODE -ge 8) { throw "robocopy fallo con codigo $LASTEXITCODE" }

Copy-Item (Join-Path $raiz "requirements.txt") $tmp

Write-Host "Subiendo a ${Vps}:${Destino}..."
& ssh @sshArgs $Vps "mkdir -p $Destino/app $Destino/logs"
if ($LASTEXITCODE -ne 0) { throw "No se pudo preparar el destino" }

& scp @sshArgs -r -q "$tmp\app\*" "${Vps}:${Destino}/app/"
if ($LASTEXITCODE -ne 0) { throw "scp de app/ fallo" }

& scp @sshArgs -q "$tmp\requirements.txt" "${Vps}:${Destino}/"
if ($LASTEXITCODE -ne 0) { throw "scp de requirements.txt fallo" }

Write-Host "Instalando dependencias y reiniciando..."
& ssh @sshArgs $Vps "cd $Destino; venv/bin/pip install -q -r requirements.txt; systemctl restart $Servicio"
if ($LASTEXITCODE -ne 0) { throw "El reinicio del servicio fallo" }

Write-Host "Comprobando..."
& ssh @sshArgs $Vps "sleep 3; curl -sf localhost:8003/health; echo"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "El health check no responde. Mira: ssh $Vps 'journalctl -u $Servicio -n 50 --no-pager'"
    exit 1
}

Remove-Item -Recurse -Force $tmp
Write-Host "Desplegado." -ForegroundColor Green
