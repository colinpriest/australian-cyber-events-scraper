@echo off
rem Six-month refresh. Stops at the first failing step and returns its exit
rem code, so a later step (status) can never hide an earlier failure.
rem Exit codes from the pipeline: 0 ok, 2 degraded (finished with failed
rem steps - see the log), anything else failed.

python scripts/oaic/OAIC_dashboard_scraper.py --from-2025
if errorlevel 1 (
    echo OAIC dashboard scrape FAILED - stopping.
    exit /b %errorlevel%
)

python pipeline.py refresh --days 180 --max-events 1000
if errorlevel 1 (
    echo Pipeline refresh did not finish cleanly ^(exit %errorlevel%^) - stopping before status.
    exit /b %errorlevel%
)

python pipeline.py status
exit /b %errorlevel%
