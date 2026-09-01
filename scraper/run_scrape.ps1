# run_scrape.ps1
# Local scheduled-task wrapper for the Travis County scraper.
#
# Why this exists: tccsearch.org (the source site) sits behind Cloudflare
# bot-detection that blocks GitHub Actions' datacenter IP ranges specifically
# -- confirmed live 2026-09-01 that the identical scraper code works fine
# from a non-datacenter connection. Rather than build any Cloudflare-bypass
# tooling (proxies, anti-detection packages, CAPTCHA solving -- a firm policy
# line), this runs the same code from this machine via Windows Task
# Scheduler instead of the cloud. Known trade-off: this only runs if this
# machine is on and awake at the scheduled time -- unlike the other 5
# counties, which run in GitHub's cloud regardless.
#
# Logs to scraper\run_scrape.log so a missed/failed run is debuggable after
# the fact instead of silently not happening.
#
# NOTE: Python's logging module writes to stderr by default. Capturing a
# native command's stderr via `2>&1` into a PowerShell variable wraps each
# line in a NativeCommandError and can abort the script even on a real
# success (confirmed live: this killed the very first run here on nothing
# but fetch.py's own startup banner). Redirect straight to the log FILE
# instead (`*>>`), which doesn't have that problem, and check $LASTEXITCODE
# for the real pass/fail signal.

$RepoRoot = "C:\Users\Xsilv\Documents\E4 Property\travis-leads"
$LogFile  = Join-Path $RepoRoot "scraper\run_scrape.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Set-Location $RepoRoot
Log "===== run_scrape.ps1 starting ====="

# Guard against the exact failure that hit the first real run
# (2026-09-01): leftover uncommitted code edits in the working tree made
# every rebase attempt fail with "cannot rebase: you have unstaged
# changes," and the script still reported the scrape itself as a
# success -- the data was scraped and committed locally but silently
# never reached GitHub. Fail loudly up front instead.
$dirty = git status --porcelain -- . ':!dashboard/records.json' ':!data/records.json'
if ($dirty) {
    Log "ABORTING: working tree has uncommitted changes outside records.json -- commit or stash them manually first, or the push will fail the same way it did 2026-09-01:"
    Log "$dirty"
    exit 1
}

Log "Running scraper/fetch.py..."
python scraper\fetch.py *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Log "fetch.py exited with code $LASTEXITCODE -- skipping commit/push, leaving records.json as last-known-good."
    exit 1
}
Log "fetch.py completed successfully."

git add dashboard/records.json data/records.json 2>>$LogFile | Out-Null

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Log "No changes to commit."
    exit 0
}

$commitMsg = "chore: update leads (local) $(Get-Date -Format 'yyyy-MM-dd HH:mm') CT"
git commit -m $commitMsg *>> $LogFile

$pushed = $false
for ($i = 1; $i -le 5; $i++) {
    Start-Sleep -Seconds (Get-Random -Minimum 2 -Maximum 10)
    git fetch origin main *>> $LogFile
    git rebase -X ours origin/main *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        git rebase --abort *>> $LogFile
        Log "Rebase failed, attempt $i"
        continue
    }
    git push origin main *>> $LogFile
    if ($LASTEXITCODE -eq 0) {
        Log "Push succeeded."
        $pushed = $true
        break
    }
    Log "Push failed, attempt $i"
}
if (-not $pushed) {
    Log "ERROR: push failed after 5 attempts. Local commit exists but is NOT on GitHub -- dashboard will not update until this is resolved."
    exit 1
}

Log "===== run_scrape.ps1 done ====="
