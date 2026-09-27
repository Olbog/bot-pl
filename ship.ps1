# Коммит и пуш с рабочего ноута: .\ship.ps1
# Описание коммита берётся из COMMIT_MSG.txt (его готовит Claude), после коммита файл удаляется.
Set-Location $PSScriptRoot
$msgFile = Join-Path $PSScriptRoot "COMMIT_MSG.txt"

git add -A
if (-not (git status --porcelain)) {
    Write-Host "Нет изменений для коммита." -ForegroundColor Yellow
    exit 0
}
Write-Host "Изменённые файлы:" -ForegroundColor Cyan
git status --short

if (Test-Path $msgFile) {
    git commit -F $msgFile
    if ($LASTEXITCODE -eq 0) { Remove-Item $msgFile }
} else {
    git commit -m "Обновление"
}
if ($LASTEXITCODE -ne 0) { Write-Host "Коммит не удался." -ForegroundColor Red; exit 1 }

git push
if ($LASTEXITCODE -ne 0) { Write-Host "Push не удался." -ForegroundColor Red; exit 1 }
Write-Host "Готово: изменения на GitHub. На сервере запусти .\deploy.ps1" -ForegroundColor Green
