# Создаёт в локальном PostgreSQL пользователя и базу для SchoolPR.
# Имя базы, пользователя и пароль берутся из .env (DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT).
# Пароль суперпользователя postgres скрипт спрашивает сам и никуда не сохраняет.
#
# Запуск из папки проекта:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_postgres.ps1

param([string]$SuperUser = 'postgres')

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) { throw "Не найден $envFile" }

$vars = @{}
foreach ($line in Get-Content $envFile -Encoding UTF8) {
    if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
        $vars[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
foreach ($key in 'DB_NAME', 'DB_USER', 'DB_PASSWORD') {
    if (-not $vars[$key]) { throw "В .env не задан $key" }
}
$dbName = $vars['DB_NAME']
$dbUser = $vars['DB_USER']
$dbPassword = $vars['DB_PASSWORD'].Replace("'", "''")
$dbHost = if ($vars['DB_HOST']) { $vars['DB_HOST'] } else { 'localhost' }
$dbPort = if ($vars['DB_PORT']) { $vars['DB_PORT'] } else { '5432' }

$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
    $psql = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin\psql.exe' -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $psql) { throw 'psql не найден. Сначала установите PostgreSQL.' }

$secure = Read-Host "Пароль суперпользователя '$SuperUser' (задавали при установке PostgreSQL)" -AsSecureString
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:PGCLIENTENCODING = 'UTF8'

try {
    $common = @('-U', $SuperUser, '-h', $dbHost, '-p', $dbPort, '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q')

    # CREATEDB нужен, чтобы Django мог создавать временную базу для тестов
    $roleSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$dbUser') THEN
        CREATE ROLE "$dbUser" LOGIN CREATEDB PASSWORD '$dbPassword';
    ELSE
        ALTER ROLE "$dbUser" WITH LOGIN CREATEDB PASSWORD '$dbPassword';
    END IF;
END
`$`$;
"@
    $roleSql | & $psql @common
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать пользователя' }
    Write-Host "Пользователь $dbUser готов."

    $exists = & $psql @common -tAc "SELECT 1 FROM pg_database WHERE datname = '$dbName'"
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить список баз' }
    if ($exists -eq '1') {
        Write-Host "База $dbName уже существует, оставляю как есть."
    } else {
        & $psql @common -c "CREATE DATABASE `"$dbName`" OWNER `"$dbUser`" ENCODING 'UTF8' TEMPLATE template0"
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать базу' }
        Write-Host "База $dbName создана."
    }
    Write-Host 'Готово. Можно возвращаться к Claude.'
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
