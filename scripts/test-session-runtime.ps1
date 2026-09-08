$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = Join-Path $taskRoot 'apps/backend'
$tests = @(
  'tests/backend/test_session_architecture.py',
  'tests/backend/test_session_desktop_services.py',
  'tests/backend/agent/test_scheduler_fire_at.py',
  'tests/backend/agent/test_scheduler_latency.py',
  'tests/backend/agent/test_scheduler_cron.py',
  'tests/backend/agent/screen_observation',
  'tests/backend/desktop_bridge/voice/test_voice_handler.py',
  'tests/backend/desktop_bridge/voice/test_voice_service.py',
  'tests/backend/desktop_bridge/voice/test_voice_http.py',
  'tests/backend/desktop_bridge/voice/test_tts_text.py'
) | ForEach-Object { Join-Path $taskRoot $_ }
& (Join-Path $taskRoot '.venv/Scripts/python.exe') -m pytest @tests -q --tb=short
exit $LASTEXITCODE
