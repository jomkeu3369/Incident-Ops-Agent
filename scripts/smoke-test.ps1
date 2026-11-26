<#
.SYNOPSIS
외부 API 키 없이 실행 중인 Incident-Ops-Agent 스택(Caddy 또는 로컬 서비스)의
핵심 안전 경계와 3대 프로토콜 상태를 검증하는 Phase 5 스모크 테스트 스크립트.

.EXAMPLE
.\scripts\smoke-test.ps1 -BaseUrl http://localhost -InternalToken $env:INTERNAL_SERVICE_TOKEN
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost",
    [string]$InternalToken = $env:INTERNAL_SERVICE_TOKEN
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

if ([string]::IsNullOrWhiteSpace($InternalToken)) {
    $envPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
    if (Test-Path $envPath) {
        $line = Get-Content $envPath | Where-Object { $_ -match "^INTERNAL_SERVICE_TOKEN=(.+)$" } | Select-Object -First 1
        if ($line -match "^INTERNAL_SERVICE_TOKEN=(.+)$") {
            $InternalToken = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}

if ([string]::IsNullOrWhiteSpace($InternalToken)) {
    throw "INTERNAL_SERVICE_TOKEN is required. Pass -InternalToken or set it in repo root .env."
}

function Invoke-Api {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Url,
        [hashtable]$Headers = @{},
        [object]$Body = $null
    )
    $methodUpper = $Method.ToUpperInvariant()
    $bodyText = if ($null -eq $Body) { $null } else { [string]$Body }
    try {
        $params = @{
            Method          = $methodUpper
            Uri             = $Url
            Headers         = $Headers
            UseBasicParsing = $true
        }
        if ($methodUpper -ne "GET" -and $methodUpper -ne "HEAD" -and -not [string]::IsNullOrEmpty($bodyText)) {
            $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes($bodyText)
            $params["ContentType"] = "application/json; charset=utf-8"
        }
        $resp = Invoke-WebRequest @params
        return @{ Status = [int]$resp.StatusCode; Content = $resp.Content }
    } catch {
        $webResp = $_.Exception.Response
        if ($null -eq $webResp) {
            throw
        }
        $statusCode = [int]$webResp.StatusCode
        $reader = New-Object System.IO.StreamReader($webResp.GetResponseStream())
        $content = $reader.ReadToEnd()
        $reader.Close()
        return @{ Status = $statusCode; Content = $content }
    }
}

function Assert-Status {
    param([string]$Name, [int]$Actual, [int]$Expected)
    if ($Actual -ne $Expected) {
        throw "[FAIL] $Name - expected HTTP $Expected, got HTTP $Actual"
    }
    Write-Host "[PASS] $Name (HTTP $Actual)" -ForegroundColor Green
}

$authHeaders = @{ "X-Internal-Token" = $InternalToken }
$stamp = Get-Date -Format "yyyyMMddHHmmss"
$smokeIncident = "INC-SMOKE-$stamp"

Write-Host "=== Incident-Ops-Agent Phase 5 Smoke Test ($BaseUrl) ===" -ForegroundColor Cyan

# 1. Fail-closed internal token check
$unauth = Invoke-Api -Method "GET" -Url "$BaseUrl/api/incidents"
Assert-Status -Name "Spring fail-closed without X-Internal-Token" -Actual $unauth.Status -Expected 401

# 2. Seeded demo incident check
$demo = Invoke-Api -Method "GET" -Url "$BaseUrl/api/incidents/INC-2026-001" -Headers $authHeaders
Assert-Status -Name "Seeded demo incident detail (INC-2026-001)" -Actual $demo.Status -Expected 200

# 3. AG-UI supervisor status
$agui = Invoke-Api -Method "GET" -Url "$BaseUrl/api/ag-ui/status"
Assert-Status -Name "Supervisor AG-UI status" -Actual $agui.Status -Expected 200

# 4. Isolated incident ledger flow (3/3 contract):
#    (a) PENDING(409) -> REJECTED(OPEN, 409)
#    (b) APPROVED + worker FAILED(hold without RESOLVED/postmortem)
#    (c) APPROVED + command mismatch(409) -> SUCCESS(200 RESOLVED + postmortem)
$approvedCmd = "kubectl rollout undo deployment/payment-service"
$createBody = @{
    incidentCode = $smokeIncident
    title        = "Smoke test payment latency"
    serviceName  = "payment-service"
    severity     = "SEV2"
    summary      = "Automated Phase 5 smoke verification"
} | ConvertTo-Json -Compress
$created = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents" -Headers $authHeaders -Body $createBody
Assert-Status -Name "Create isolated smoke incident ($smokeIncident)" -Actual $created.Status -Expected 201

$planId = "PLAN-SMOKE-$stamp"
$planBody = @{
    planId               = $planId
    steps                = @(
        @{
            stepNo           = 1
            description      = "Rollback payment-service deployment"
            condition        = "Operator approval confirmed"
            actionCommand    = $approvedCmd
            evidenceChunkIds = @("RUNBOOK-01")
        }
    )
    verificationCriteria = "502 rate returns to 0%"
    createdBy            = "smoke-tester"
} | ConvertTo-Json -Depth 5 -Compress
$planRes = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/plans" -Headers $authHeaders -Body $planBody
Assert-Status -Name "Register remediation plan ($planId)" -Actual $planRes.Status -Expected 201

# 4a. Unapproved PENDING (409) -> REJECTED (reverts to OPEN) -> REJECTED completion blocked (409)
$rejectApprovalId = "APR-REJ-$stamp"
$rejectApprovalBody = @{
    approvalId = $rejectApprovalId
    planId     = $planId
    stepNo     = 1
    proposedBy = "smoke-tester"
} | ConvertTo-Json -Compress
$rejAprRes = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals" -Headers $authHeaders -Body $rejectApprovalBody
Assert-Status -Name "Create PENDING approval for rejection test ($rejectApprovalId)" -Actual $rejAprRes.Status -Expected 201

$rejectCompleteBody = @{
    approvalId          = $rejectApprovalId
    actionCommand       = $approvedCmd
    executedBy          = "smoke-tester"
    executionStatus     = "SUCCESS"
    executionOutput     = "[simulated] should be blocked"
    verificationSummary = "502 rate 0.0%"
    rootCause           = "smoke test simulated leak"
    timelineMarkdown    = "- smoke verified"
    preventionItems     = "- add automated gate"
} | ConvertTo-Json -Compress

$blockedPending = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/complete" -Headers $authHeaders -Body $rejectCompleteBody
Assert-Status -Name "Unapproved PENDING completion blocked (409)" -Actual $blockedPending.Status -Expected 409

$rejectDecisionBody = @{
    decision   = "REJECTED"
    approvedBy = "smoke-reviewer"
} | ConvertTo-Json -Compress
$rejected = Invoke-Api -Method "PATCH" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals/$rejectApprovalId" -Headers $authHeaders -Body $rejectDecisionBody
Assert-Status -Name "Reject remediation approval ($rejectApprovalId)" -Actual $rejected.Status -Expected 200
$rejectedObj = $rejected.Content | ConvertFrom-Json
if ($rejectedObj.decision -ne "REJECTED") {
    throw "[FAIL] Expected decision=REJECTED, got $($rejectedObj.decision)"
}
$detailAfterReject = Invoke-Api -Method "GET" -Url "$BaseUrl/api/incidents/$smokeIncident" -Headers $authHeaders
Assert-Status -Name "Fetch incident after rejection" -Actual $detailAfterReject.Status -Expected 200
$detailAfterRejectObj = $detailAfterReject.Content | ConvertFrom-Json
if ($detailAfterRejectObj.incident.status -ne "OPEN") {
    throw "[FAIL] Expected incident status=OPEN after rejection, got $($detailAfterRejectObj.incident.status)"
}
Write-Host "[PASS] Rejected approval reverted incident status to OPEN" -ForegroundColor Green

$blockedRejected = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/complete" -Headers $authHeaders -Body $rejectCompleteBody
Assert-Status -Name "Rejected approval completion blocked (409)" -Actual $blockedRejected.Status -Expected 409

# 4b. Approved worker failure (executionStatus=FAILED) holds incident without RESOLVED or postmortem
$failApprovalId = "APR-FAIL-$stamp"
$failApprovalBody = @{
    approvalId = $failApprovalId
    planId     = $planId
    stepNo     = 1
    proposedBy = "smoke-tester"
} | ConvertTo-Json -Compress
$failAprRes = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals" -Headers $authHeaders -Body $failApprovalBody
Assert-Status -Name "Create PENDING approval for worker failure hold test ($failApprovalId)" -Actual $failAprRes.Status -Expected 201

$approveBody = @{
    decision   = "APPROVED"
    approvedBy = "smoke-approver"
} | ConvertTo-Json -Compress
$approvedForFail = Invoke-Api -Method "PATCH" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals/$failApprovalId" -Headers $authHeaders -Body $approveBody
Assert-Status -Name "Approve remediation for worker failure test ($failApprovalId)" -Actual $approvedForFail.Status -Expected 200

$failedCompleteBody = @{
    approvalId      = $failApprovalId
    actionCommand   = $approvedCmd
    executedBy      = "smoke-worker"
    executionStatus = "FAILED"
    executionOutput = "[simulated] worker rollback execution timed out"
} | ConvertTo-Json -Compress
$workerFailed = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/complete" -Headers $authHeaders -Body $failedCompleteBody
Assert-Status -Name "Worker failure recorded without resolving incident" -Actual $workerFailed.Status -Expected 200
$workerFailedObj = $workerFailed.Content | ConvertFrom-Json
if ($workerFailedObj.incidentStatus -eq "RESOLVED" -or $null -ne $workerFailedObj.postMortem) {
    throw "[FAIL] Worker failure must hold incident (got status=$($workerFailedObj.incidentStatus))"
}
$noPostMortem = Invoke-Api -Method "GET" -Url "$BaseUrl/api/incidents/$smokeIncident/postmortem" -Headers $authHeaders
Assert-Status -Name "Postmortem absent while worker failure is held (404)" -Actual $noPostMortem.Status -Expected 404
Write-Host "[PASS] Worker FAILED execution held incident at $($workerFailedObj.incidentStatus) with no postmortem" -ForegroundColor Green

# 4c. Approved command mismatch blocked (409) -> Approved SUCCESS resolves incident with postmortem
$approvalId = "APR-OK-$stamp"
$approvalBody = @{
    approvalId = $approvalId
    planId     = $planId
    stepNo     = 1
    proposedBy = "smoke-tester"
} | ConvertTo-Json -Compress
$aprRes = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals" -Headers $authHeaders -Body $approvalBody
Assert-Status -Name "Create PENDING approval for completion test ($approvalId)" -Actual $aprRes.Status -Expected 201

$approved = Invoke-Api -Method "PATCH" -Url "$BaseUrl/api/incidents/$smokeIncident/approvals/$approvalId" -Headers $authHeaders -Body $approveBody
Assert-Status -Name "Approve remediation ($approvalId)" -Actual $approved.Status -Expected 200

$tamperedCompleteBody = @{
    approvalId          = $approvalId
    actionCommand       = "kubectl delete deployment/payment-service"
    executedBy          = "smoke-tester"
    executionStatus     = "SUCCESS"
    executionOutput     = "[simulated] tampered command"
    verificationSummary = "502 rate 0.0%"
    rootCause           = "smoke test simulated leak"
    timelineMarkdown    = "- smoke verified"
    preventionItems     = "- add automated gate"
} | ConvertTo-Json -Compress
$blockedTampered = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/complete" -Headers $authHeaders -Body $tamperedCompleteBody
Assert-Status -Name "Approved command mismatch blocked (409)" -Actual $blockedTampered.Status -Expected 409

$completeBody = @{
    approvalId          = $approvalId
    actionCommand       = $approvedCmd
    executedBy          = "smoke-tester"
    executionStatus     = "SUCCESS"
    executionOutput     = "[simulated] executed rollback"
    verificationSummary = "502 rate 0.0%"
    rootCause           = "smoke test simulated leak"
    timelineMarkdown    = "- smoke verified"
    preventionItems     = "- add automated gate"
} | ConvertTo-Json -Compress

$completed = Invoke-Api -Method "POST" -Url "$BaseUrl/api/incidents/$smokeIncident/complete" -Headers $authHeaders -Body $completeBody
Assert-Status -Name "Approved completion resolves incident" -Actual $completed.Status -Expected 200
$completedObj = $completed.Content | ConvertFrom-Json
if ($completedObj.incidentStatus -ne "RESOLVED" -or $null -eq $completedObj.postMortem) {
    throw "[FAIL] Expected incidentStatus=RESOLVED with postMortem, got $($completedObj.incidentStatus)"
}
Write-Host "[PASS] Incident transitioned to RESOLVED with postmortem" -ForegroundColor Green

# 5. Next.js SSR & BFF non-leak verification
$bff = Invoke-Api -Method "GET" -Url "$BaseUrl/bff/incidents/INC-2026-001"
Assert-Status -Name "Next.js BFF incident proxy" -Actual $bff.Status -Expected 200
if ($bff.Content.Contains($InternalToken)) {
    throw "[FAIL] INTERNAL_SERVICE_TOKEN leaked in BFF response!"
}
Write-Host "[PASS] BFF response does not leak INTERNAL_SERVICE_TOKEN" -ForegroundColor Green

Write-Host "=== ALL PHASE 5 SMOKE CHECKS PASSED ===" -ForegroundColor Cyan
