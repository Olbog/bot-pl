# Обновление бота на сервере: .\deploy.ps1
# git pull -> пересборка контейнера -> тесты -> последние строки лога
Set-Location $PSScriptRoot

Write-Host "1/4 git pull" -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Write-Host "git pull не удался." -ForegroundColor Red; exit 1 }

Write-Host "2/4 сборка и перезапуск" -ForegroundColor Cyan
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "Сборка не удалась." -ForegroundColor Red; exit 1 }

Start-Sleep -Seconds 3
Write-Host "3/4 тесты" -ForegroundColor Cyan
docker compose exec -T bot pytest -q
$testsOk = ($LASTEXITCODE -eq 0)

Write-Host "4/4 лог" -ForegroundColor Cyan
docker compose logs --tail 15

if ($testsOk) { Write-Host "Готово: бот обновлён, тесты зелёные." -ForegroundColor Green }
else { Write-Host "Бот перезапущен, но тесты упали — пришли вывод Claude." -ForegroundColor Red; exit 1 }
